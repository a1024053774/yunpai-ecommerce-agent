from __future__ import annotations

from collections.abc import Callable
from typing import Any

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import AdminPrincipal
from .business.demand_facts import DemandFactRebuildRequest
from .business.forecast_demo import (
    ensure_three_year_demo,
    ensure_three_year_demo_data,
    ensure_three_year_demo_plan,
)
from .business.forecasting import ForecastRequest, ForecastRunRequest
from .business.inventory_planning import InventoryPlanCreateRequest, InventoryPlanningPolicy
from .service import AgentService


class ForecastResolveRequest(BaseModel):
    """Resolve an operator-selected scope to real or clearly marked demo data."""

    model_config = ConfigDict(extra="forbid")

    store_id: str | None = Field(default=None, min_length=1, max_length=128)
    sku_id: str | None = Field(default=None, min_length=1, max_length=128)
    horizon_days: Literal[7, 14, 30] = 7

    @model_validator(mode="after")
    def require_complete_scope(self) -> "ForecastResolveRequest":
        if (self.store_id is None) != (self.sku_id is None):
            raise ValueError("forecast_scope_requires_store_and_sku")
        return self


class ForecastDemoPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon_days: Literal[7, 14, 30] = 30


def _real_sales_dates(
    service: AgentService,
    *,
    tenant_id: str,
    store_id: str,
    sku_id: str,
) -> list[date]:
    demand_service = service.operations.demand_facts
    policy = demand_service.policy
    zone = timezone(timedelta(hours=8))
    closed_through = demand_service.now_provider().astimezone(zone).date() - timedelta(days=1)
    dates: set[date] = set()
    for order in service.operations.orders.list_orders(
        tenant_id,
        store_id=store_id,
        limit=100_000,
    ):
        if (
            str(order["order_status"]) in policy.excluded_order_statuses
            or str(order["payment_status"]) not in policy.included_payment_statuses
            or not any(str(line["sku_id"]) == sku_id for line in order["lines"])
        ):
            continue
        placed_at = datetime.fromisoformat(str(order["placed_at"]).replace("Z", "+00:00"))
        business_date = placed_at.astimezone(zone).date()
        if business_date <= closed_through:
            dates.add(business_date)
    return sorted(dates)


def _resolve_source(
    service: AgentService,
    *,
    tenant_id: str,
    payload: ForecastResolveRequest,
) -> dict[str, Any]:
    requested_scope = (
        {"store_id": payload.store_id, "sku_id": payload.sku_id}
        if payload.store_id is not None and payload.sku_id is not None
        else None
    )
    real_dates = (
        _real_sales_dates(
            service,
            tenant_id=tenant_id,
            store_id=payload.store_id,
            sku_id=payload.sku_id,
        )
        if requested_scope is not None
        else []
    )
    if requested_scope is not None and len(real_dates) >= 14:
        service.operations.demand_facts.rebuild(
            tenant_id,
            store_id=payload.store_id,
            sku_id=payload.sku_id,
            start_date=real_dates[0],
            end_date=real_dates[-1],
        )
        return {
            "source_type": "real",
            "virtual": False,
            "production_claim": False,
            "requested_scope": requested_scope,
            "effective_scope": requested_scope,
            "real_history_day_count": len(real_dates),
            "fallback_reason": None,
            "demo_sales_day_count": None,
        }
    demo = ensure_three_year_demo_data(service, tenant_id=tenant_id)
    return {
        "source_type": "demo",
        "virtual": True,
        "production_claim": False,
        "requested_scope": requested_scope,
        "effective_scope": {"store_id": demo["store_id"], "sku_id": demo["sku_id"]},
        "real_history_day_count": len(real_dates),
        "fallback_reason": "real_history_insufficient",
        "demo_sales_day_count": demo["sales_day_count"],
    }


def build_forecasting_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/forecasting", tags=["forecasting"])

    @router.post("/preview")
    def preview_forecast(
        payload: ForecastRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.forecasting.preview(admin.tenant_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "forecasting.preview.generated",
            admin.admin_id,
            f"{payload.store_id}:{payload.sku_id}",
            {
                "store_id": payload.store_id,
                "warehouse_id": payload.warehouse_id,
                "sku_id": payload.sku_id,
                "horizon_days": payload.horizon_days,
                "status": result["status"],
                "persisted": result["persisted"],
                "external_order_created": result["external_order_created"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/demand/rebuild")
    def rebuild_demand(
        payload: DemandFactRebuildRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.demand_facts.rebuild(
                admin.tenant_id,
                store_id=payload.store_id,
                sku_id=payload.sku_id,
                start_date=payload.start_date,
                end_date=payload.end_date,
                mode=payload.mode,
                source_gap_dates=payload.source_gap_dates,
                stockout_statuses=payload.stockout_statuses,
            )
            service.db.audit(
                "forecasting.demand.rebuilt",
                admin.admin_id,
                f"{payload.store_id}:{payload.sku_id}",
                {
                    "store_id": payload.store_id,
                    "sku_id": payload.sku_id,
                    "fact_version": result["fact_version"],
                    "write_status": result["write_status"],
                    "source_watermark": result["source_watermark"],
                },
                admin.tenant_id,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/demand")
    def list_demand(
        store_id: str = Query(min_length=1, max_length=128),
        sku_id: str = Query(min_length=1, max_length=128),
        start_date: date | None = None,
        end_date: date | None = None,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if start_date and end_date and end_date < start_date:
            raise HTTPException(status_code=422, detail="demand_fact_invalid_date_range")
        return service.operations.demand_facts.list_response(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            start_date=start_date,
            end_date=end_date,
        )

    @router.post("/resolve-and-run")
    def resolve_and_run(
        payload: ForecastResolveRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = _resolve_source(service, tenant_id=admin.tenant_id, payload=payload)
            if result["virtual"]:
                result["forecast"] = ensure_three_year_demo(
                    service,
                    tenant_id=admin.tenant_id,
                    horizon_days=payload.horizon_days,
                )["run"]
            else:
                scope = result["effective_scope"]
                result["forecast"] = service.operations.forecasting.run(
                    admin.tenant_id,
                    ForecastRunRequest(
                        store_id=scope["store_id"],
                        sku_id=scope["sku_id"],
                        horizon_days=payload.horizon_days,
                    ),
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "forecasting.source_resolved",
            admin.admin_id,
            f"{payload.store_id or 'demo'}:{payload.sku_id or 'demo'}",
            {
                "requested_scope": result["requested_scope"],
                "effective_scope": result["effective_scope"],
                "source_type": result["source_type"],
                "real_history_day_count": result["real_history_day_count"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/resolve-source")
    def resolve_source(
        payload: ForecastResolveRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = _resolve_source(service, tenant_id=admin.tenant_id, payload=payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "forecasting.source_resolved",
            admin.admin_id,
            f"{payload.store_id or 'demo'}:{payload.sku_id or 'demo'}",
            {
                "requested_scope": result["requested_scope"],
                "effective_scope": result["effective_scope"],
                "source_type": result["source_type"],
                "real_history_day_count": result["real_history_day_count"],
            },
            admin.tenant_id,
        )
        return result

    @router.post("/demo-plan")
    def create_demo_plan(
        payload: ForecastDemoPlanRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = ensure_three_year_demo_plan(
                service,
                tenant_id=admin.tenant_id,
                horizon_days=payload.horizon_days,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service.db.audit(
            "forecasting.demo_plan.generated",
            admin.admin_id,
            result["plan"]["plan_id"],
            {
                "virtual": True,
                "store_id": result["store_id"],
                "sku_id": result["sku_id"],
                "warehouse_id": result["warehouse_id"],
                "external_order_created": False,
            },
            admin.tenant_id,
        )
        return result

    @router.post("/runs")
    def create_run(
        payload: ForecastRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.operations.forecasting.run(admin.tenant_id, payload)
            service.db.audit(
                "forecasting.run.created",
                admin.admin_id,
                result["run_id"],
                {
                    "store_id": payload.store_id,
                    "sku_id": payload.sku_id,
                    "champion_model": result["champion_model"],
                    "forecast_horizon": payload.horizon_days,
                    "data_hash": result["data_hash"],
                },
                admin.tenant_id,
            )
            return result
        except ValueError as exc:
            code = str(exc)
            status = 404 if code == "forecast_run_not_found" else 409
            raise HTTPException(status_code=status, detail=code) from exc

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        try:
            return service.operations.forecasting.get_run(admin.tenant_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/shadow")
    def shadow_run(run_id: str, admin: AdminPrincipal = Depends(require_admin)) -> dict[str, Any]:
        try:
            return service.operations.forecast_shadow.evaluate(admin.tenant_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/skus/{sku_id}/forecast")
    def latest_forecast(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.forecasting.latest_run(
            admin.tenant_id, store_id=store_id, sku_id=sku_id
        )
        if result is None:
            raise HTTPException(status_code=404, detail="forecast_run_not_found")
        return result

    @router.get("/skus/{sku_id}/backtest")
    def latest_backtest(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        run = service.operations.forecasting.latest_run(
            admin.tenant_id, store_id=store_id, sku_id=sku_id
        )
        return {
            "store_id": store_id,
            "sku_id": sku_id,
            "run_id": run["run_id"] if run else None,
            "champion_model": run["champion_model"] if run else None,
            "champion_reason": run["champion_reason"] if run else None,
            "metrics": run["metrics"] if run else {},
            "backtest_summary": run["backtest_summary"] if run else [],
            "backtests": run["backtests"] if run else [],
        }

    @router.put("/policies/{sku_id}")
    def update_forecast_policy(
        sku_id: str,
        payload: ForecastRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if payload.sku_id != sku_id:
            raise HTTPException(status_code=422, detail="sku_scope_mismatch")
        try:
            result = service.operations.forecasting.upsert_policy(admin.tenant_id, payload)
            service.db.audit(
                "forecasting.policy.updated",
                admin.admin_id,
                result["policy_id"],
                {"store_id": payload.store_id, "sku_id": payload.sku_id, "policy_version": result["policy_version"]},
                admin.tenant_id,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/planning-policies/{sku_id}")
    def update_planning_policy(
        sku_id: str,
        payload: InventoryPlanningPolicy,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if payload.sku_id != sku_id:
            raise HTTPException(status_code=422, detail="sku_scope_mismatch")
        try:
            result = service.operations.inventory_planning.upsert_policy(admin.tenant_id, payload)
            service.db.audit(
                "forecasting.planning_policy.updated",
                admin.admin_id,
                result["policy_id"],
                {"store_id": payload.store_id, "sku_id": payload.sku_id, "warehouse_id": payload.warehouse_id, "policy_version": result["policy_version"]},
                admin.tenant_id,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/skus/{sku_id}/inventory-plan")
    def create_inventory_plan(
        sku_id: str,
        payload: InventoryPlanCreateRequest,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            run = service.operations.forecasting.get_run(admin.tenant_id, payload.forecast_run_id)
            if run["sku_id"] != sku_id or run["store_id"] != store_id:
                raise ValueError("forecast_scope_mismatch")
            result = service.operations.inventory_planning.create_plan(
                admin.tenant_id,
                forecast_run_id=payload.forecast_run_id,
                warehouse_id=payload.warehouse_id,
            )
            service.db.audit(
                "forecasting.inventory_plan.created",
                admin.admin_id,
                result["plan_id"],
                {
                    "store_id": store_id,
                    "sku_id": sku_id,
                    "warehouse_id": payload.warehouse_id,
                    "forecast_run_id": payload.forecast_run_id,
                    "status": result["status"],
                },
                admin.tenant_id,
            )
            return result
        except ValueError as exc:
            status = 404 if str(exc) in {"forecast_run_not_found", "inventory_balance_not_found", "inventory_policy_not_found"} else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/skus/{sku_id}/inventory-plan")
    def latest_inventory_plan(
        sku_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        warehouse_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = service.operations.inventory_planning.latest_plan(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            warehouse_id=warehouse_id,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="inventory_plan_not_found")
        return result

    @router.get("/risks")
    def forecasting_risks(
        store_id: str | None = Query(default=None, max_length=128),
        warehouse_id: str | None = Query(default=None, max_length=128),
        risk_level: Literal["critical", "high", "medium", "replenishment_due", "overstock", "healthy"] | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "risks": service.operations.inventory_planning.list_risks(
                admin.tenant_id,
                store_id=store_id,
                warehouse_id=warehouse_id,
                risk_level=risk_level,
                limit=limit,
            )
        }

    return router
