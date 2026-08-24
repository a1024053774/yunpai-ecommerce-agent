from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .profit import (
    CATEGORY_LAYER,
    ExpenseCategory,
    LedgerEntryInput,
    ProfitError,
    ProfitLayer,
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
        result = call(profit.projection, admin.tenant_id, store_id, period, scope)
        if "finance:final_profit:read" not in admin.capabilities:
            result = result.model_copy(
                update={
                    "final": result.final.model_copy(
                        update={"amount": None, "restricted": True}
                    )
                }
            )
            service.db.audit(
                "profit.final_profit.read_denied",
                admin.admin_id,
                store_id,
                {"period": period, "scope": scope.value},
                admin.tenant_id,
            )
        return result

    @router.get("/reconciliation")
    def reconciliation(
        store_id: str = Query(min_length=1, max_length=128),
        period: str = Query(min_length=7, max_length=7),
        scope: ProfitScope = Query(default=ProfitScope.FORMAL),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(profit.reconcile, admin.tenant_id, store_id, period, scope)

    @router.get("/ledger/entries")
    def ledger_entries(
        store_id: str = Query(min_length=1, max_length=128),
        sku_id: str | None = Query(default=None, min_length=1, max_length=128),
        period: str | None = Query(default=None, min_length=7, max_length=7),
        scope: ProfitScope | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        """只读单品费用下钻；财务最终层金额对无权限管理员脱敏（#11）。"""
        can_read_final = "finance:final_profit:read" in admin.capabilities
        rows = profit.list_entries(
            admin.tenant_id,
            store_id,
            sku_id=sku_id,
            period=period,
            scope=scope,
        )
        masked = False
        for row in rows:
            category = row["category"]
            layer = CATEGORY_LAYER.get(ExpenseCategory(category))
            if layer is ProfitLayer.FINAL and not can_read_final:
                row["amount"] = None
                row["restricted"] = True
                masked = True
        if masked:
            service.db.audit(
                "profit.ledger.entries.final_denied",
                admin.admin_id,
                store_id,
                {"sku_id": sku_id, "period": period, "scope": scope.value if scope else None},
                admin.tenant_id,
            )
        return rows

    return router
