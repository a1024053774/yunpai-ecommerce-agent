"""生成阶段的分支决策：`/v1/chat` 与 `/v1/chat/stream` 的唯一事实源。

审计 P0-2（`docs/AUDIT_ROUTING_EVOLVABILITY_20260807.md` 1.3、2.1-2）确认
`service._generation_deltas` 手抄了 `graph.generate` 的三条分支并已漂移。修法是消灭
手抄：两条通道都消费 `plan_generation`，非流式（graph 侧）行为为准。

本模块不得 import `graph.py`——graph 的 `generate` 节点反向消费本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..database import Database
from ..llm import ModelError, ModelUnavailableError
from ..prompts import SYSTEM_PROMPT, build_messages
from ..schemas import DraftOrigin
from ..text_utils import normalize_text
from ..tokens import count_tokens, truncate_history


NO_EVIDENCE_DRAFT = "当前知识库中没有足够信息，我会为您转人工客服进一步核对。"

BRANCH_NO_EVIDENCE = "no_evidence"
BRANCH_APPROVED_DIRECT = "approved_direct"
BRANCH_MODEL = "model"


@dataclass(frozen=True)
class GenerationPlan:
    """一次生成的分支决定。`branch != BRANCH_MODEL` 时不得调用生成模型。"""

    branch: str
    model_fallback: bool
    trace_step: str | None = None
    text: str | None = None
    messages: list[dict[str, str]] | None = None
    history_meta: dict[str, int | bool] | None = None
    budget_trace: str | None = None
    prompt_variant: str | None = None
    scene: str | None = None
    scene_applied: bool = False
    evidence_source: str | None = None


@dataclass(frozen=True)
class ModelFailureRecovery:
    draft: str
    model_fallback: bool
    retry_advised: bool
    trace_step: str


def draft_origin_for_plan(plan: GenerationPlan) -> DraftOrigin:
    if plan.branch == BRANCH_APPROVED_DIRECT:
        return "approved_knowledge"
    if plan.branch == BRANCH_MODEL:
        return "model"
    return "fallback"


def map_scene(intent: str) -> str:
    """按意图映射生成阶段场景（M3 四套 Prompt 接入）。

    return/refund/return_exchange/after_sales → aftersale_policy
    product → product_recommend
    competitor → competitor_analysis
    其余 → customer_service
    """
    intent = (intent or "").lower()
    if intent in {"return", "refund", "return_exchange", "after_sales", "invoice"}:
        return "aftersale_policy"
    if intent in {"product", "inventory", "price_promo"}:
        return "product_recommend"
    if "competitor" in intent:
        return "competitor_analysis"
    return "customer_service"


def context_budgets(
    settings: Settings,
    question: str,
    system_prompt: str,
) -> tuple[int, int]:
    total = int(settings.model_context_limit_tokens * settings.context_budget_ratio)
    available = max(
        0,
        total - count_tokens(system_prompt) - count_tokens(question),
    )
    knowledge_budget = available * 6 // 10
    return knowledge_budget, available - knowledge_budget


def budgeted_history(
    state: dict[str, Any],
    *,
    db: Database,
    settings: Settings,
    system_prompt: str,
) -> tuple[list[dict[str, Any]], dict[str, int | bool], int]:
    knowledge_budget, history_budget = context_budgets(
        settings,
        state["normalized_input"],
        system_prompt,
    )
    history = db.recent_messages(
        state["session_id"],
        settings.session_history_limit,
    )
    selected, meta = truncate_history(history, budget_tokens=history_budget)
    return selected, meta, knowledge_budget


def verified_tool_result(state: dict[str, Any]) -> dict[str, Any] | None:
    tool_result = state.get("tool_result") or {}
    return tool_result if tool_result.get("postcondition_met") else None


def has_media_observation(state: dict[str, Any]) -> bool:
    media = state.get("media_evidence") or {}
    return media.get("status") == "applied" and bool(media.get("description"))


def approved_direct_document(
    state: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any] | None:
    """审核话术直出资格（CONTRIBUTING 第 10 节「快速路径资格」）。

    必须同时成立：模型给出的 approved_knowledge_reuse 决定、`evolution:` 人工审核
    来源、normalize 后问题与请求完全相等。分数过线不是资格。
    """
    documents = state.get("retrieved") or []
    if has_media_observation(state):
        return None
    if not documents or not settings.rag_direct_approved_answer:
        return None
    top_document = documents[0]
    if (state.get("decision") or {}).get("reason") != "approved_knowledge_reuse":
        return None
    if not str(top_document.get("source") or "").startswith("evolution:"):
        return None
    if normalize_text(top_document["question"]) != normalize_text(
        state["normalized_input"]
    ):
        return None
    return top_document


def recover_model_failure(
    state: dict[str, Any],
    error: ModelError,
    *,
    db: Database,
) -> ModelFailureRecovery:
    verified_result = verified_tool_result(state)
    if verified_result:
        recovery = ModelFailureRecovery(
            draft="操作已完成，业务系统已经确认处理结果。",
            model_fallback=True,
            retry_advised=False,
            trace_step="generate:verified_result_fallback",
        )
    elif isinstance(error, ModelUnavailableError):
        recovery = ModelFailureRecovery(
            draft="",
            model_fallback=True,
            retry_advised=True,
            trace_step="generate:model_temporarily_unavailable",
        )
    else:
        recovery = ModelFailureRecovery(
            draft="当前模型暂时不可用，我会为您转人工客服，避免给出不准确的信息。",
            model_fallback=True,
            retry_advised=False,
            trace_step="generate:fallback",
        )
    db.audit(
        "model.failure",
        "system",
        state["trace_id"],
        {"error_type": type(error).__name__, "error": str(error)[:300]},
        state["tenant_id"],
    )
    return recovery


def _with_scene_prompt(
    messages: list[dict[str, str]],
    state: dict[str, Any],
    *,
    settings: Settings,
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], str | None, bool]:
    # M3 场景 Prompt 接入：按 intent 映射场景，叠加防幻觉指令（RAG_SCENE_PROMPTS 默认开）
    if not settings.rag_scene_prompts or not messages or not documents:
        return messages, None, False
    scene = map_scene(state.get("intent") or "")
    try:
        from ..knowledge_engine.prompt_templates import render_prompt

        context_text = "\n".join(
            document.get("answer") or document.get("question") or ""
            for document in documents[:5]
        )
        scene_instructions = render_prompt(
            scene, context_text, state["normalized_input"]
        )
    except (ValueError, ImportError):
        return messages, scene, False  # 场景注入失败不阻塞回答（保持原 SYSTEM_PROMPT）
    patched = [
        {
            "role": "system",
            "content": (
                f"{messages[0]['content']}\n\n【本会话场景指令】\n{scene_instructions}"
            ),
        },
        *messages[1:],
    ]
    return patched, scene, True


def plan_generation(
    state: dict[str, Any],
    *,
    settings: Settings,
    db: Database,
) -> GenerationPlan:
    documents = state.get("retrieved") or []
    tool_result = verified_tool_result(state)
    if not documents and not tool_result and not has_media_observation(state):
        return GenerationPlan(
            branch=BRANCH_NO_EVIDENCE,
            model_fallback=True,
            trace_step="generate:no_evidence",
            text=NO_EVIDENCE_DRAFT,
        )
    approved = approved_direct_document(state, settings=settings)
    if approved is not None:
        return GenerationPlan(
            branch=BRANCH_APPROVED_DIRECT,
            model_fallback=False,
            trace_step="generate:approved_knowledge",
            text=approved["answer"],
            evidence_source=str(approved["source"]),
        )
    history, history_meta, knowledge_budget = budgeted_history(
        state,
        db=db,
        settings=settings,
        system_prompt=SYSTEM_PROMPT,
    )
    prompt_variant = (state.get("intent_routing") or {}).get("prompt_variant")
    messages = build_messages(
        question=state["normalized_input"],
        documents=documents,
        context=state["context_bundle"],
        history=history,
        verified_tool_result=tool_result,
        knowledge_budget_tokens=knowledge_budget,
        prompt_variant=prompt_variant,
    )
    messages, scene, scene_applied = _with_scene_prompt(
        messages,
        state,
        settings=settings,
        documents=documents,
    )
    return GenerationPlan(
        branch=BRANCH_MODEL,
        model_fallback=False,
        messages=messages,
        history_meta=history_meta,
        budget_trace=(
            f"context:budget:kept{history_meta['kept']}"
            f"/dropped{history_meta['dropped']}"
        ),
        prompt_variant=prompt_variant,
        scene=scene,
        scene_applied=scene_applied,
    )
