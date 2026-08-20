from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProfitLayer(StrEnum):
    SALES = "sales"
    OPERATING = "operating"
    FINAL = "final"


class ProfitScope(StrEnum):
    FORMAL = "formal"
    DEMO = "demo"


class RevenueRecognitionBasis(StrEnum):
    SIGNED_RECEIPT = "signed_receipt"


class ExpenseCategory(StrEnum):
    # 销售层
    SIGNED_REVENUE = "signed_receipt_revenue"
    REFUND_OFFSET = "refund_offset"
    PURCHASE_COST = "purchase_cost"
    DIRECT_PRODUCT_COST = "direct_product_cost"
    # 经营层
    PACKAGING_COST = "packaging_cost"
    WAREHOUSE_PICKING_COST = "warehouse_picking_cost"
    FORWARD_LOGISTICS_COST = "forward_logistics_cost"
    REVERSE_LOGISTICS_COST = "reverse_logistics_cost"
    RETURN_INBOUND_COST = "return_inbound_cost"
    PLATFORM_FEE = "platform_fee"
    PLATFORM_COMMISSION = "platform_commission"
    TECH_SERVICE_FEE = "tech_service_fee"
    TRANSPORT_INSURANCE = "transport_insurance"
    PIT_FEE = "pit_fee"
    SERVICE_FEE = "service_fee"
    KOL_COMMISSION = "kol_commission"
    GIFT_COST = "gift_cost"
    PUBLIC_WELFARE_COST = "public_welfare_cost"
    ADVERTISING_COST = "advertising_cost"
    REFURBISHMENT_COST = "refurbishment_cost"
    # 财务最终层
    TAX_COST = "tax_cost"
    PERIOD_ADJUSTMENT = "period_adjustment"


# 单一权威源（D-035）：类别 -> 唯一利润层，防止跨层重复扣除。
CATEGORY_LAYER: dict[ExpenseCategory, ProfitLayer] = {
    ExpenseCategory.SIGNED_REVENUE: ProfitLayer.SALES,
    ExpenseCategory.REFUND_OFFSET: ProfitLayer.SALES,
    ExpenseCategory.PURCHASE_COST: ProfitLayer.SALES,
    ExpenseCategory.DIRECT_PRODUCT_COST: ProfitLayer.SALES,
    ExpenseCategory.PACKAGING_COST: ProfitLayer.OPERATING,
    ExpenseCategory.WAREHOUSE_PICKING_COST: ProfitLayer.OPERATING,
    ExpenseCategory.FORWARD_LOGISTICS_COST: ProfitLayer.OPERATING,
    ExpenseCategory.REVERSE_LOGISTICS_COST: ProfitLayer.OPERATING,
    ExpenseCategory.RETURN_INBOUND_COST: ProfitLayer.OPERATING,
    ExpenseCategory.PLATFORM_FEE: ProfitLayer.OPERATING,
    ExpenseCategory.PLATFORM_COMMISSION: ProfitLayer.OPERATING,
    ExpenseCategory.TECH_SERVICE_FEE: ProfitLayer.OPERATING,
    ExpenseCategory.TRANSPORT_INSURANCE: ProfitLayer.OPERATING,
    ExpenseCategory.PIT_FEE: ProfitLayer.OPERATING,
    ExpenseCategory.SERVICE_FEE: ProfitLayer.OPERATING,
    ExpenseCategory.KOL_COMMISSION: ProfitLayer.OPERATING,
    ExpenseCategory.GIFT_COST: ProfitLayer.OPERATING,
    ExpenseCategory.PUBLIC_WELFARE_COST: ProfitLayer.OPERATING,
    ExpenseCategory.ADVERTISING_COST: ProfitLayer.OPERATING,
    ExpenseCategory.REFURBISHMENT_COST: ProfitLayer.OPERATING,
    ExpenseCategory.TAX_COST: ProfitLayer.FINAL,
    ExpenseCategory.PERIOD_ADJUSTMENT: ProfitLayer.FINAL,
}

# 金额方向（D-035 单一事实源）：收入为正、退款/成本为负；期间调整允许双向。
CATEGORY_SIGN: dict[ExpenseCategory, Literal["positive", "negative", "either"]] = {
    ExpenseCategory.SIGNED_REVENUE: "positive",
    ExpenseCategory.REFUND_OFFSET: "negative",
    ExpenseCategory.PURCHASE_COST: "negative",
    ExpenseCategory.DIRECT_PRODUCT_COST: "negative",
    ExpenseCategory.PACKAGING_COST: "negative",
    ExpenseCategory.WAREHOUSE_PICKING_COST: "negative",
    ExpenseCategory.FORWARD_LOGISTICS_COST: "negative",
    ExpenseCategory.REVERSE_LOGISTICS_COST: "negative",
    ExpenseCategory.RETURN_INBOUND_COST: "negative",
    ExpenseCategory.PLATFORM_FEE: "negative",
    ExpenseCategory.PLATFORM_COMMISSION: "negative",
    ExpenseCategory.TECH_SERVICE_FEE: "negative",
    ExpenseCategory.TRANSPORT_INSURANCE: "negative",
    ExpenseCategory.PIT_FEE: "negative",
    ExpenseCategory.SERVICE_FEE: "negative",
    ExpenseCategory.KOL_COMMISSION: "negative",
    ExpenseCategory.GIFT_COST: "negative",
    ExpenseCategory.PUBLIC_WELFARE_COST: "negative",
    ExpenseCategory.ADVERTISING_COST: "negative",
    ExpenseCategory.REFURBISHMENT_COST: "negative",
    ExpenseCategory.TAX_COST: "negative",
    ExpenseCategory.PERIOD_ADJUSTMENT: "either",
}


# 正式口径必需费用（缺失任一即该层“暂不可核算”，缺失不补零）。
REQUIRED_CATEGORIES_BY_LAYER: dict[ProfitLayer, frozenset[ExpenseCategory]] = {
    ProfitLayer.SALES: frozenset(
        {
            ExpenseCategory.SIGNED_REVENUE,
            ExpenseCategory.PURCHASE_COST,
            ExpenseCategory.DIRECT_PRODUCT_COST,
        }
    ),
    ProfitLayer.OPERATING: frozenset(
        {
            ExpenseCategory.SIGNED_REVENUE,
            ExpenseCategory.PURCHASE_COST,
            ExpenseCategory.DIRECT_PRODUCT_COST,
            ExpenseCategory.PLATFORM_FEE,
            ExpenseCategory.ADVERTISING_COST,
            ExpenseCategory.FORWARD_LOGISTICS_COST,
            ExpenseCategory.TRANSPORT_INSURANCE,
        }
    ),
    ProfitLayer.FINAL: frozenset(
        {
            ExpenseCategory.SIGNED_REVENUE,
            ExpenseCategory.PURCHASE_COST,
            ExpenseCategory.DIRECT_PRODUCT_COST,
            ExpenseCategory.PLATFORM_FEE,
            ExpenseCategory.ADVERTISING_COST,
            ExpenseCategory.FORWARD_LOGISTICS_COST,
            ExpenseCategory.TRANSPORT_INSURANCE,
            ExpenseCategory.TAX_COST,
        }
    ),
}


def layer_required_categories(layer: ProfitLayer) -> frozenset[ExpenseCategory]:
    return REQUIRED_CATEGORIES_BY_LAYER[ProfitLayer(layer)]


def _require_code(value: str, *, error: str, max_length: int = 128) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(error)
    return value


def content_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProfitPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    revenue_recognition_basis: RevenueRecognitionBasis = (
        RevenueRecognitionBasis.SIGNED_RECEIPT
    )
    required_categories: dict[ProfitLayer, list[ExpenseCategory]] | None = None

    @field_validator("policy_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _require_code(value, error="invalid_profit_policy_version")


class LedgerEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str
    period: str
    category: ExpenseCategory
    scope: ProfitScope
    amount: str
    currency: str = "CNY"
    source_kind: Literal["actual", "manual", "demo"]
    sku_id: str | None = None
    order_id: str | None = None
    mapping_version: str = "v1"
    entry_key: str
    source_reference: str | None = None

    @field_validator("store_id", "period", "mapping_version", "entry_key")
    @classmethod
    def validate_codes(cls, value: str) -> str:
        return _require_code(value, error="invalid_ledger_code")

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_code(value, error="invalid_source_reference", max_length=512)

    @model_validator(mode="after")
    def validate_entry(self) -> "LedgerEntryInput":
        category = ExpenseCategory(self.category)
        if category in {
            ExpenseCategory.SIGNED_REVENUE,
            ExpenseCategory.REFUND_OFFSET,
        } and not self.order_id:
            raise ValueError("revenue_entry_requires_order")
        if self.scope is ProfitScope.FORMAL and self.source_kind == "demo":
            raise ValueError("demo_source_cannot_enter_formal_scope")
        if self.scope is ProfitScope.DEMO and self.source_kind != "demo":
            raise ValueError("formal_source_cannot_enter_demo_scope")
        if (
            self.scope is ProfitScope.FORMAL
            and category
            in {
                ExpenseCategory.SIGNED_REVENUE,
                ExpenseCategory.REFUND_OFFSET,
            }
            and self.source_kind != "actual"
        ):
            raise ValueError("formal_revenue_requires_actual_source")
        try:
            amount = Decimal(self.amount)
        except Exception as exc:
            raise ValueError("invalid_ledger_amount") from exc
        if not amount.is_finite() or amount == 0:
            raise ValueError("invalid_ledger_amount")
        sign = CATEGORY_SIGN[category]
        if sign == "positive" and amount <= 0:
            raise ValueError("ledger_amount_must_be_positive")
        if sign == "negative" and amount >= 0:
            raise ValueError("ledger_amount_must_be_negative")
        return self


class LayerProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: ProfitLayer
    status: Literal["available", "missing"]
    amount: str | None = None
    label: str
    missing_fields: list[str] = Field(default_factory=list)


class ProfitProjectionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    store_id: str
    period: str
    scope: ProfitScope
    policy_version: str
    sales: LayerProjection
    operating: LayerProjection
    final: LayerProjection
    demo_labels: bool


class ReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    entry_key: str | None = None
    message: str


class ReconciliationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    store_id: str
    period: str
    scope: ProfitScope
    entry_count: int
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    double_count_ok: bool
