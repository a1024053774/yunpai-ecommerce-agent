from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


# 权威状态机（D-035 单一事实源）：
#   draft -> awaiting_confirmation -> confirmed -> in_transit -> received
#   draft -> cancelled
#   confirmed / in_transit -> overdue
LEGAL_TRANSITIONS: dict[PurchaseOrderStatus, frozenset[PurchaseOrderStatus]] = {
    PurchaseOrderStatus.DRAFT: frozenset(
        {PurchaseOrderStatus.AWAITING_CONFIRMATION, PurchaseOrderStatus.CANCELLED}
    ),
    PurchaseOrderStatus.AWAITING_CONFIRMATION: frozenset(
        {PurchaseOrderStatus.CONFIRMED, PurchaseOrderStatus.CANCELLED}
    ),
    PurchaseOrderStatus.CONFIRMED: frozenset(
        {PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.OVERDUE}
    ),
    PurchaseOrderStatus.IN_TRANSIT: frozenset(
        {PurchaseOrderStatus.RECEIVED, PurchaseOrderStatus.OVERDUE}
    ),
    PurchaseOrderStatus.RECEIVED: frozenset(),
    PurchaseOrderStatus.CANCELLED: frozenset(),
    PurchaseOrderStatus.OVERDUE: frozenset(),
}

# 进入这些状态代表外部执行事实，必须有操作者来源引用，禁止系统自动推断。
EXTERNAL_STATE_REQUIRES_SOURCE: frozenset[PurchaseOrderStatus] = frozenset(
    {
        PurchaseOrderStatus.IN_TRANSIT,
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.OVERDUE,
    }
)


def legal_transition(
    current: PurchaseOrderStatus, target: PurchaseOrderStatus
) -> bool:
    return target in LEGAL_TRANSITIONS[PurchaseOrderStatus(current)]


class OrderDraftMode(StrEnum):
    FORMAL = "formal"
    DEMO = "demo"


def _require_code(value: str, *, error: str, max_length: int = 128) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(error)
    return value


class OrderDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str
    sku_id: str
    material_no: str | None = None
    supplier_ref: str | None = None
    recommended_qty: int = Field(ge=1)
    unit_cost: str | None = None
    currency: str = "CNY"
    promised_delivery_at: str | None = None
    forecast_run_ref: str | None = None
    inventory_snapshot_ref: str | None = None
    policy_ref: str | None = None
    source_summary: str = Field(min_length=1, max_length=512)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    mode: OrderDraftMode = OrderDraftMode.FORMAL

    @field_validator("store_id", "sku_id", "supplier_ref", "forecast_run_ref",
                    "inventory_snapshot_ref", "policy_ref")
    @classmethod
    def validate_codes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_order_draft_code")

    @field_validator("material_no")
    @classmethod
    def validate_material_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_material_no", max_length=256)

    @field_validator("unit_cost")
    @classmethod
    def validate_unit_cost(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_unit_cost", max_length=64)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return _require_code(value, error="invalid_currency", max_length=8)

    @field_validator("promised_delivery_at")
    @classmethod
    def validate_delivery(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_promised_delivery_at", max_length=64)


class OrderConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    confirmed_qty: int = Field(ge=0)
    supplier_ref: str | None = None
    promised_delivery_at: str | None = None

    @field_validator("supplier_ref")
    @classmethod
    def validate_supplier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_supplier_ref")

    @field_validator("promised_delivery_at")
    @classmethod
    def validate_delivery(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_promised_delivery_at", max_length=64)


class OrderStatusAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    to_status: PurchaseOrderStatus
    source_ref: str | None = None
    note: str | None = Field(default=None, max_length=256)

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_source_ref", max_length=512)


class OrderEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    order_draft_id: str
    from_status: str
    to_status: str
    actor: str
    source_ref: str | None = None
    note: str | None = None
    created_at: str


class OrderDraftView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_draft_id: str
    tenant_id: str
    store_id: str
    sku_id: str
    material_no: str
    supplier_ref: str | None
    recommended_qty: int
    confirmed_qty: int | None
    unit_cost: str | None
    currency: str
    promised_delivery_at: str | None
    forecast_run_ref: str | None
    inventory_snapshot_ref: str | None
    policy_ref: str | None
    source_summary: str
    assumptions: list[str]
    missing_fields: list[str]
    mode: OrderDraftMode
    status: PurchaseOrderStatus
    version: int
    created_by: str
    confirmed_by: str | None
    created_at: str
    updated_at: str
    unsent_label: str
    events: list[OrderEventView] = Field(default_factory=list)


class DraftGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    mode: OrderDraftMode
    material_no: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    reason: str | None = None
