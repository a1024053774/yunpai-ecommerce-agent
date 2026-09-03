from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .alibaba_1688 import (
    Alibaba1688Error,
    Alibaba1688RemoteError,
)
from .business.channel_availability import AvailabilityScope
from .auth import AdminPrincipal
from .service import AgentService


def build_alibaba_1688_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/integrations/alibaba-1688",
        tags=["alibaba-1688"],
    )

    @router.get("/capabilities")
    def capabilities(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.alibaba_1688.capabilities(admin.tenant_id)

    @router.post("/authorize")
    def authorize(
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        try:
            return service.alibaba_1688.begin_authorization(
                admin.tenant_id, store_id
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/oauth/callback")
    def oauth_callback(
        code: str = Query(min_length=1),
        state: str | None = Query(default=None, min_length=16),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.complete_authorization(code, state)
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def read_params(
        store_id: str,
        cursor: str | None,
        limit: int,
        modify_start_time: str | None,
        modify_end_time: str | None,
    ) -> dict[str, Any]:
        return {
            "store_id": store_id,
            "cursor": cursor,
            "limit": limit,
            "modify_start_time": modify_start_time,
            "modify_end_time": modify_end_time,
        }

    @router.get("/orders")
    def orders(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=20),
        modify_start_time: str | None = Query(default=None, max_length=64),
        modify_end_time: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            batch = service.alibaba_1688.list_orders(
                admin.tenant_id,
                **read_params(
                    store_id, cursor, limit, modify_start_time, modify_end_time
                ),
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return batch.model_dump(mode="json")

    @router.get("/orders/{order_id}")
    def order_detail(
        order_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            record = service.alibaba_1688.get_order(
                admin.tenant_id,
                store_id=store_id,
                order_id=order_id,
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return record.model_dump(mode="json")

    @router.get("/products")
    def products(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=20),
        modify_start_time: str | None = Query(default=None, max_length=64),
        modify_end_time: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            batch = service.alibaba_1688.list_products(
                admin.tenant_id,
                **read_params(
                    store_id, cursor, limit, modify_start_time, modify_end_time
                ),
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return batch.model_dump(mode="json")

    @router.get("/products/{product_id}")
    def product_detail(
        product_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            record = service.alibaba_1688.get_product(
                admin.tenant_id,
                store_id=store_id,
                product_id=product_id,
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return record.model_dump(mode="json")

    @router.get("/products/{product_id}/availability")
    def product_availability(
        product_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.get_product_availability(
                admin.tenant_id,
                store_id=store_id,
                product_id=product_id,
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/availability")
    def persisted_availability(
        store_id: str = Query(min_length=1, max_length=128),
        product_id: str | None = Query(default=None, max_length=128),
        sku_id: str | None = Query(default=None, max_length=128),
        scope: AvailabilityScope | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.list_persisted_availability(
                admin.tenant_id,
                store_id=store_id,
                product_id=product_id,
                sku_id=sku_id,
                scope=scope,
                limit=limit,
            )
        except (Alibaba1688Error, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/availability/{product_id}")
    def persisted_product_availability(
        product_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            result = service.alibaba_1688.get_persisted_availability(
                admin.tenant_id,
                store_id=store_id,
                product_id=product_id,
            )
        except (Alibaba1688Error, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="channel availability not found")
        return result

    @router.post("/sync/orders")
    def sync_orders(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=20),
        modify_start_time: str | None = Query(default=None, max_length=64),
        modify_end_time: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.sync_orders(
                admin.tenant_id,
                **read_params(
                    store_id, cursor, limit, modify_start_time, modify_end_time
                ),
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/sync/products")
    def sync_products(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=20),
        modify_start_time: str | None = Query(default=None, max_length=64),
        modify_end_time: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.sync_products(
                admin.tenant_id,
                **read_params(
                    store_id, cursor, limit, modify_start_time, modify_end_time
                ),
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/sync/availability")
    def sync_availability(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=20),
        modify_start_time: str | None = Query(default=None, max_length=64),
        modify_end_time: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_1688.sync_availability(
                admin.tenant_id,
                **read_params(
                    store_id, cursor, limit, modify_start_time, modify_end_time
                ),
            )
        except Alibaba1688Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Alibaba1688RemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
