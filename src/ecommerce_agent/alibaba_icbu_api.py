from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .alibaba_icbu import AlibabaIcbuError, AlibabaIcbuRemoteError
from .auth import AdminPrincipal
from .service import AgentService


def build_alibaba_icbu_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/integrations/alibaba-icbu",
        tags=["alibaba-icbu"],
    )

    @router.get("/capabilities")
    def capabilities(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.alibaba_icbu.capabilities(admin.tenant_id)

    @router.post("/authorize")
    def authorize(
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        try:
            return service.alibaba_icbu.begin_authorization(
                admin.tenant_id, store_id
            )
        except AlibabaIcbuError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/oauth/callback")
    def oauth_callback(
        code: str = Query(min_length=1),
        state: str = Query(min_length=16),
    ) -> dict[str, Any]:
        try:
            return service.alibaba_icbu.complete_authorization(code, state)
        except AlibabaIcbuError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AlibabaIcbuRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/products")
    def products(
        store_id: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=30, ge=1, le=30),
        gmt_modified_from: str | None = Query(default=None, max_length=64),
        gmt_modified_to: str | None = Query(default=None, max_length=64),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            batch = service.alibaba_icbu.list_products(
                admin.tenant_id,
                store_id=store_id,
                cursor=cursor,
                limit=limit,
                gmt_modified_from=gmt_modified_from,
                gmt_modified_to=gmt_modified_to,
            )
        except AlibabaIcbuError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlibabaIcbuRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return batch.model_dump(mode="json")

    @router.get("/products/{encrypted_product_id}")
    def product_detail(
        encrypted_product_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            batch = service.alibaba_icbu.product_detail(
                admin.tenant_id,
                store_id=store_id,
                encrypted_product_id=encrypted_product_id,
            )
        except AlibabaIcbuError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlibabaIcbuRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return batch.model_dump(mode="json")

    @router.get("/products/{plain_product_id}/inventory")
    def product_inventory(
        plain_product_id: str = Path(min_length=1, max_length=128),
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        try:
            batch = service.alibaba_icbu.product_inventory(
                admin.tenant_id,
                store_id=store_id,
                plain_product_id=plain_product_id,
            )
        except AlibabaIcbuError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlibabaIcbuRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return batch.model_dump(mode="json")

    return router
