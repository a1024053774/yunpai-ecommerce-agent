"""Shared customer-service decision and generation primitives."""

from .generation import (
    BRANCH_APPROVED_DIRECT,
    BRANCH_MODEL,
    BRANCH_NO_EVIDENCE,
    GenerationPlan,
    draft_origin_for_plan,
    plan_generation,
    recover_model_failure,
)

__all__ = [
    "BRANCH_APPROVED_DIRECT",
    "BRANCH_MODEL",
    "BRANCH_NO_EVIDENCE",
    "GenerationPlan",
    "draft_origin_for_plan",
    "plan_generation",
    "recover_model_failure",
]
