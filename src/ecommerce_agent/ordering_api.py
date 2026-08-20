from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import AdminPrincipal
from .ordering import (
    OrderConfirmRequest,
    OrderDraftCreate,
    OrderingError,
    OrderingService,
    OrderStatusAdvanceRequest,
    PurchaseOrderStatus,
)


def build_ordering_router(
    service: Any,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/ordering", tags=["ordering"])
    ordering: OrderingService = service.ordering

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OrderingError as exc:
            detail = str(exc)
            status_code = 404 if detail.endswith("not_found") else 409
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @router.post("/drafts")
    def create_draft(
        payload: OrderDraftCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.create_draft,
            admin.tenant_id,
            payload.store_id,
            admin.admin_id,
            payload,
        )

    @router.get("/drafts")
    def list_drafts(
        store_id: str = Query(min_length=1, max_length=128),
        status: PurchaseOrderStatus | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.list,
            admin.tenant_id,
            store_id,
            status=status,
            limit=limit,
        )

    @router.get("/drafts/{order_draft_id}")
    def get_draft(
        order_draft_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(ordering.get, admin.tenant_id, store_id, order_draft_id)

    @router.post("/drafts/{order_draft_id}/submit")
    def submit_draft(
        order_draft_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.submit_for_confirmation,
            admin.tenant_id,
            store_id,
            order_draft_id,
            admin.admin_id,
        )

    @router.post("/drafts/{order_draft_id}/confirm")
    def confirm_draft(
        order_draft_id: str,
        payload: OrderConfirmRequest,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.confirm,
            admin.tenant_id,
            store_id,
            order_draft_id,
            admin.admin_id,
            payload,
        )

    @router.post("/drafts/{order_draft_id}/cancel")
    def cancel_draft(
        order_draft_id: str,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.cancel,
            admin.tenant_id,
            store_id,
            order_draft_id,
            admin.admin_id,
        )

    @router.post("/drafts/{order_draft_id}/status")
    def advance_status(
        order_draft_id: str,
        payload: OrderStatusAdvanceRequest,
        store_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ):
        return call(
            ordering.advance_status,
            admin.tenant_id,
            store_id,
            order_draft_id,
            admin.admin_id,
            payload,
        )

    return router
