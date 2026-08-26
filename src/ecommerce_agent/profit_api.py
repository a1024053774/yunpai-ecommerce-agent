from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import (
    CAPABILITY_FINANCE_LEDGER_WRITE,
    CAPABILITY_FINANCE_POLICY_WRITE,
    AdminPrincipal,
)
from .profit import (
    CATEGORY_LAYER,
    ExpenseCategory,
    LedgerEntryInput,
    ProfitError,
    ProfitLayer,
    ProfitPolicyInput,
    ProfitScope,
    ProfitService,
    category_is_final,
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

    def require_capability(admin: AdminPrincipal, capability: str) -> None:
        if capability not in admin.capabilities:
            raise HTTPException(
                status_code=403, detail=f"capability_denied:{capability}"
            )

    @router.post("/policies")
    def register_policy(
        payload: ProfitPolicyInput,
        admin: AdminPrincipal = Depends(require_admin),
    ):
        require_capability(admin, CAPABILITY_FINANCE_POLICY_WRITE)
        with service.db._write_lock, service.db.connect() as conn:
            result = call(
                profit.register_policy,
                admin.tenant_id,
                payload,
                connection=conn,
            )
            service.db.audit(
                "profit.policy.registered",
                admin.admin_id,
                payload.policy_version,
                {
                    "policy_version": payload.policy_version,
                    "revenue_recognition_basis": result["revenue_recognition_basis"],
                },
                admin.tenant_id,
                connection=conn,
            )
        return result

    @router.post("/ledger/entries")
    def record_entry(
        payload: LedgerEntryInput,
        admin: AdminPrincipal = Depends(require_admin),
    ):
        require_capability(admin, CAPABILITY_FINANCE_LEDGER_WRITE)
        with service.db._write_lock, service.db.connect() as conn:
            result = call(
                profit.record_entry,
                admin.tenant_id,
                payload,
                connection=conn,
            )
            service.db.audit(
                "profit.ledger.entry_recorded",
                admin.admin_id,
                result["entry_id"],
                {
                    "store_id": payload.store_id,
                    "period": payload.period,
                    "category": payload.category.value,
                    "scope": payload.scope.value,
                    # 财务最终层金额不进审计，避免无权限管理员从审计详情读回（#11/P0-1）。
                    "amount": (
                        None
                        if category_is_final(payload.category)
                        else payload.amount
                    ),
                    "entry_key": payload.entry_key,
                },
                admin.tenant_id,
                connection=conn,
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
        result = call(profit.reconcile, admin.tenant_id, store_id, period, scope)
        if "finance:final_profit:read" not in admin.capabilities:
            masked = False
            for issue in result.issues:
                if issue.is_final and issue.amount is not None:
                    issue.amount = None
                    masked = True
            if masked:
                service.db.audit(
                    "profit.reconciliation.final_denied",
                    admin.admin_id,
                    store_id,
                    {"period": period, "scope": scope.value},
                    admin.tenant_id,
                )
        return result

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
