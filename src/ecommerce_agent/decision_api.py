from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .auth import AdminPrincipal
from .profit import ProfitError, ProfitScope


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    period: str = Field(min_length=7, max_length=7)
    scope: ProfitScope = ProfitScope.FORMAL


def _marketing_available(service: Any, tenant_id: str, store_id: str) -> dict[str, Any]:
    rows = service.operations.marketing.list_performance(
        tenant_id, store_id=store_id, limit=1
    )
    return {"available": bool(rows), "record_count": len(rows)}


def build_decision_router(
    service: Any,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/decision", tags=["decision"])

    @router.post("/suggestions")
    def suggestions(
        payload: DecisionRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "tenant_id": admin.tenant_id,
            "store_id": payload.store_id,
            "period": payload.period,
            "scope": payload.scope.value,
        }
        try:
            facts["profit_projection"] = service.profit.projection(
                admin.tenant_id, payload.store_id, payload.period, payload.scope
            ).model_dump()
        except ProfitError as exc:
            facts["profit_projection"] = {
                "status": "unavailable",
                "reason": str(exc),
            }
        try:
            facts["profit_reconciliation"] = service.profit.reconcile(
                admin.tenant_id, payload.store_id, payload.period, payload.scope
            ).model_dump()
        except ProfitError as exc:
            facts["profit_reconciliation"] = {
                "status": "unavailable",
                "reason": str(exc),
            }
        facts["ordering_drafts"] = [
            item.model_dump()
            for item in service.ordering.list(
                admin.tenant_id, payload.store_id, limit=50
            )
        ]
        facts["inventory_risks"] = service.operations.inventory_plans.list_risks(
            admin.tenant_id, store_id=payload.store_id, limit=20
        )
        facts["marketing"] = _marketing_available(
            service, admin.tenant_id, payload.store_id
        )
        result = service.decision_advisor.suggest(facts)
        service.db.audit(
            "decision.suggestions.requested",
            admin.admin_id,
            payload.store_id,
            {
                "period": payload.period,
                "scope": payload.scope.value,
                "available": result.available,
                "reason": result.reason,
                "facts_digest": result.facts_digest,
            },
            admin.tenant_id,
        )
        return {
            "available": result.available,
            "reason": result.reason,
            "suggestions": [item.model_dump() for item in result.suggestions],
            "facts_digest": result.facts_digest,
        }

    return router
