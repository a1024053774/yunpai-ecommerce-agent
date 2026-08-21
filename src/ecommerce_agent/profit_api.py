from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .profit import (
    LedgerEntryInput,
    ProfitError,
    ProfitPolicyInput,
    ProfitScope,
    ProfitService,
)


def build_profit_router(
    service: Any,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/profit", tags=["profit"])
    profit: ProfitService = service.profit

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ProfitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/policies")
    def register_policy(
        payload: ProfitPolicyInput,
        admin: AdminPrincipal = Depends(require_admin),
    ):
        result = call(profit.register_policy, admin.tenant_id, payload)
        service.db.audit(
            "profit.policy.registered",
            admin.admin_id,
            payload.policy_version,
            {
                "policy_version": payload.policy_version,
                "revenue_recognition_basis": result["revenue_recognition_basis"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/ledger/entries")
    def record_entry(
        payload: LedgerEntryInput,
        admin: AdminPrincipal = Depends(require_admin),
    ):
        result = call(profit.record_entry, admin.tenant_id, payload)
        service.db.audit(
            "profit.ledger.entry_recorded",
            admin.admin_id,
            result["entry_id"],
            {
                "store_id": payload.store_id,
                "period": payload.period,
                "category": payload.category.value,
                "scope": payload.scope.value,
                "amount": payload.amount,
                "entry_key": payload.entry_key,
            },
            admin.tenant_id,
        )
        return result

    @router.get("/projection")
    def projection(
        store_id: str = Query(min_length=1, max_length=128),
        period: str = Query(min_length=7, max_length=7),
        scope: ProfitScope = Query(default=ProfitScope.FORMAL),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(profit.projection, admin.tenant_id, store_id, period, scope)

    @router.get("/reconciliation")
    def reconciliation(
        store_id: str = Query(min_length=1, max_length=128),
        period: str = Query(min_length=7, max_length=7),
        scope: ProfitScope = Query(default=ProfitScope.FORMAL),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(profit.reconcile, admin.tenant_id, store_id, period, scope)

    return router
