from __future__ import annotations

from collections.abc import Callable
from typing import Any

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .business.demand_facts import DemandFactRebuildRequest
from .business.forecasting import ForecastRequest, ForecastRunRequest
from .business.inventory_planning import InventoryPlanCreateRequest, InventoryPlanningPolicy
from .service import AgentService


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
        facts = service.operations.demand_facts.list_facts(
            admin.tenant_id,
            store_id=store_id,
            sku_id=sku_id,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "store_id": store_id,
            "sku_id": sku_id,
            "policy_version": service.operations.demand_facts.policy.policy_version,
            "timezone": service.operations.demand_facts.policy.timezone,
            "facts": facts,
            "quality": service.operations.demand_facts._quality(facts),
        }

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
        return {
            "store_id": store_id,
            "sku_id": sku_id,
            "backtests": service.operations.forecasting.list_backtests(
                admin.tenant_id, store_id=store_id, sku_id=sku_id
            ),
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
        risk_level: Literal["critical", "high", "medium", "replenishment_due", "healthy"] | None = None,
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
