"""M10-R 门禁 #10 — 结构化经营建议入口（模型只解释固化事实，不修改任何数值）。

- 输入为已固化事实 JSON（利润投影/对账/订购单/库存风险/营销可用性）；
- 复用 ``ModelGateway.generate_json`` 输出结构化建议（建议/依据/数据缺口/
  确认人/下一步）；
- MODEL_ENABLED=false、无模型、超时或输出不合法时，显式返回
  ``available=false`` + 机器可读 reason，绝不回退到规则式语义建议；
- 服务只读，不写 ledger、利润、完整度、对账或业务状态。
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


_AMOUNT_PATTERN = re.compile(r"\d+\.\d{2}")


class DecisionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion: str = Field(min_length=1, max_length=300)
    basis: str = Field(min_length=1, max_length=600)
    data_gaps: list[str] = Field(default_factory=list, max_length=20)
    owner: str = Field(min_length=1, max_length=120)
    next_step: str = Field(min_length=1, max_length=300)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    amount_refs: list[str] = Field(default_factory=list, max_length=20)


class DecisionSuggestionResult(BaseModel):
    available: bool
    reason: str | None = None
    suggestions: list[DecisionSuggestion] = Field(default_factory=list)
    facts_digest: str | None = None


class ModelGatewayProtocol(Protocol):
    @property
    def settings(self) -> Any: ...

    def generate_json(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> dict[str, Any]: ...


SYSTEM_PROMPT = (
    "你是经营决策建议助手。只能基于给定的经营事实 JSON 解释并提出结构化建议，"
    "不得编造数值，不得修改任何事实，不得把缺失费用当 0，不得把演示参数当正式口径。"
    "始终使用简体中文输出。"
    '输出严格 JSON：{"suggestions":[{"suggestion":"建议做什么","basis":"依据（引用给定事实）",'
    '"data_gaps":["缺口"],"owner":"需谁确认","next_step":"下一步",'
    '"evidence_refs":["只能从 evidence_catalog 中选取的引用"],'
    '"amount_refs":["字段点路径=数值，如 profit.sales.amount=500.00"]}]}。'
    "evidence_refs 必须全部来自给定 evidence_catalog；amount_refs 用 字段=数值 格式且"
    "必须与给定事实精确一致；没有可引用证据时 evidence_refs/amount_refs 可为空数组。"
)


def _facts_digest(facts: dict[str, Any]) -> str:
    canonical = json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """压缩为模型可稳定消费的只读事实摘要（不复制冗长文案/标识）。"""

    compact: dict[str, Any] = {
        "store_id": facts.get("store_id"),
        "period": facts.get("period"),
        "scope": facts.get("scope"),
    }
    projection = facts.get("profit_projection")
    if isinstance(projection, dict):
        compact["profit"] = {
            layer: {
                "status": projection.get(layer, {}).get("status"),
                "amount": projection.get(layer, {}).get("amount"),
                "missing_fields": projection.get(layer, {}).get("missing_fields", []),
            }
            for layer in ("sales", "operating", "final")
        }
    else:
        compact["profit"] = {"status": "unavailable"}
    reconciliation = facts.get("profit_reconciliation")
    if isinstance(reconciliation, dict):
        compact["reconciliation"] = {
            "double_count_ok": reconciliation.get("double_count_ok"),
            "entry_count": reconciliation.get("entry_count"),
            "issue_codes": [
                issue.get("code")
                for issue in reconciliation.get("issues", [])
                if isinstance(issue, dict)
            ],
        }
    else:
        compact["reconciliation"] = {"status": "unavailable"}
    drafts = facts.get("ordering_drafts")
    if isinstance(drafts, list):
        compact["ordering_drafts"] = [
            {
                key: item.get(key)
                for key in (
                    "status",
                    "recommended_qty",
                    "confirmed_qty",
                    "material_no",
                    "unsent_label",
                )
            }
            for item in drafts
            if isinstance(item, dict)
        ]
    risks = facts.get("inventory_risks")
    if isinstance(risks, list):
        compact["inventory_risks"] = [
            {
                "sku_id": item.get("sku_id"),
                "risk_level": item.get("risk_level") or item.get("severity"),
                "summary": str(item.get("summary") or item.get("reason") or "")[:80],
            }
            for item in risks
            if isinstance(item, dict)
        ]
    marketing = facts.get("marketing")
    if isinstance(marketing, dict):
        compact["marketing_available"] = bool(marketing.get("available"))
    return compact


def _evidence_catalog(facts: dict[str, Any]) -> list[str]:
    """从压缩事实生成可被模型引用的证据 ID 清单（确定性）。"""

    catalog: list[str] = []
    profit = facts.get("profit")
    if isinstance(profit, dict):
        for layer in ("sales", "operating", "final"):
            layer_facts = profit.get(layer)
            if isinstance(layer_facts, dict):
                for field in ("status", "amount", "missing_fields"):
                    catalog.append(f"profit:{layer}:{field}")
    reconciliation = facts.get("reconciliation")
    if isinstance(reconciliation, dict):
        catalog.append("reconciliation:double_count_ok")
        catalog.append("reconciliation:entry_count")
        for code in reconciliation.get("issue_codes", []):
            catalog.append(f"reconciliation:issue:{code}")
    for draft in facts.get("ordering_drafts", []):
        if isinstance(draft, dict) and draft.get("status"):
            catalog.append(f"draft:{draft.get('status')}:{draft.get('recommended_qty')}")
    for risk in facts.get("inventory_risks", []):
        if isinstance(risk, dict) and risk.get("sku_id"):
            catalog.append(f"risk:{risk.get('sku_id')}:{risk.get('risk_level')}")
    if "marketing_available" in facts:
        catalog.append("marketing:available")
    return sorted(set(catalog))


def _resolve_path(facts: dict[str, Any], field: str) -> Any:
    node: Any = facts
    for part in str(field).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _amount_key(value: Any) -> str | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return format(amount.normalize(), "f")


def _fact_amount_keys(facts: dict[str, Any]) -> set[str]:
    """收集压缩事实中所有可解析为金额的值（归一化）。"""

    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif node is not None and not isinstance(node, bool):
            key = _amount_key(node)
            if key is not None:
                keys.add(key)

    walk(facts)
    return keys


def _validate_suggestion(
    suggestion: DecisionSuggestion,
    *,
    catalog: list[str],
    facts: dict[str, Any],
) -> None:
    """硬校验：证据引用必须在 catalog 内，数值引用必须与事实一致。"""

    catalog_set = set(catalog)
    if any(ref not in catalog_set for ref in suggestion.evidence_refs):
        raise ValueError("evidence_ref_not_in_catalog")
    for amount_ref in suggestion.amount_refs:
        field, separator, value = str(amount_ref).partition("=")
        if not separator:
            raise ValueError("amount_ref_format_invalid")
        actual = _resolve_path(facts, field)
        if actual is None or str(actual) != value:
            raise ValueError("amount_ref_mismatch")
    # 数值声称绑定：basis 中带两位小数的金额类数字必须与事实金额一致，
    # 防止模型在 basis 里编造未绑定金额（胡磊 P1-2）。
    legit_amounts = _fact_amount_keys(facts)
    for raw in _AMOUNT_PATTERN.findall(suggestion.basis):
        key = _amount_key(raw)
        if key is None or key not in legit_amounts:
            raise ValueError("basis_amount_not_bound")


class DecisionAdvisorService:
    def __init__(self, model: ModelGatewayProtocol | None) -> None:
        self._model = model

    def suggest(self, facts: dict[str, Any]) -> DecisionSuggestionResult:
        digest = _facts_digest(facts)
        model = self._model
        if model is None:
            return DecisionSuggestionResult(
                available=False, reason="model_unavailable", facts_digest=digest
            )
        try:
            enabled = bool(model.settings.model_enabled)
        except (AttributeError, TypeError):
            enabled = False
        if not enabled:
            return DecisionSuggestionResult(
                available=False, reason="model_unavailable", facts_digest=digest
            )
        compact = _compact_facts(facts)
        catalog = _evidence_catalog(compact)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_type": "decision_suggestion",
                        "facts": compact,
                        "evidence_catalog": catalog,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            raw = model.generate_json(messages, max_tokens=1200)
        except Exception as exc:  # ModelError / 超时 / 网络失败
            message = str(exc).lower()
            reason = (
                "model_unavailable" if "disabled" in message else "model_error"
            )
            return DecisionSuggestionResult(
                available=False, reason=reason, facts_digest=digest
            )
        if not isinstance(raw, dict):
            return DecisionSuggestionResult(
                available=False, reason="model_output_invalid", facts_digest=digest
            )
        items = raw.get("suggestions")
        if not isinstance(items, list) or not items:
            return DecisionSuggestionResult(
                available=False, reason="model_output_invalid", facts_digest=digest
            )
        try:
            suggestions = [DecisionSuggestion.model_validate(item) for item in items]
        except (ValidationError, TypeError, ValueError):
            return DecisionSuggestionResult(
                available=False, reason="model_output_invalid", facts_digest=digest
            )
        try:
            for suggestion in suggestions:
                _validate_suggestion(
                    suggestion, catalog=catalog, facts=compact
                )
        except ValueError:
            return DecisionSuggestionResult(
                available=False, reason="model_output_invalid", facts_digest=digest
            )
        return DecisionSuggestionResult(
            available=True,
            suggestions=suggestions,
            facts_digest=digest,
        )
