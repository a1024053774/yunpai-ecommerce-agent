from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import AdminPrincipal
from .business.forecasting import ForecastRequest
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

    return router
