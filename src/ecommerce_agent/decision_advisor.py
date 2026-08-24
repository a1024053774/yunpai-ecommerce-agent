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
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DecisionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion: str = Field(min_length=1, max_length=300)
    basis: str = Field(min_length=1, max_length=600)
    data_gaps: list[str] = Field(default_factory=list, max_length=20)
    owner: str = Field(min_length=1, max_length=120)
    next_step: str = Field(min_length=1, max_length=300)


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
    '"data_gaps":["缺口"],"owner":"需谁确认","next_step":"下一步"}]}。'
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
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_type": "decision_suggestion",
                        "facts": _compact_facts(facts),
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
        return DecisionSuggestionResult(
            available=True,
            suggestions=suggestions,
            facts_digest=digest,
        )
