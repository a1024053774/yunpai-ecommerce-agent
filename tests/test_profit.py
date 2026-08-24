from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.profit import (
    CATEGORY_LAYER,
    ExpenseCategory,
    LedgerEntryInput,
    ProfitError,
    ProfitLayer,
    ProfitPolicyInput,
    ProfitScope,
    ProfitService,
)


TENANT = "tenant-a"
STORE = "store-1"
PERIOD = "2026-08"


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "profit.sqlite3")
    db.initialize()
    return db


def make_service(tmp_path: Path) -> ProfitService:
    return ProfitService(make_db(tmp_path))


def register_default_policy(service: ProfitService) -> None:
    service.register_policy(
        TENANT,
        ProfitPolicyInput(policy_version="v1"),
    )


def entry(
    category: ExpenseCategory,
    amount: str,
    *,
    order_id: str | None = None,
    scope: ProfitScope = ProfitScope.FORMAL,
    entry_key: str | None = None,
) -> LedgerEntryInput:
    return LedgerEntryInput(
        store_id=STORE,
        period=PERIOD,
        category=category,
        scope=scope,
        amount=amount,
        source_kind=(
            "demo"
            if scope is ProfitScope.DEMO
            else "actual"
            if order_id is not None
            else "manual"
        ),
        order_id=order_id,
        entry_key=entry_key or f"{category.value}:{order_id or 'store'}",
    )


def seed_delivered_order(service: ProfitService, order_id: str) -> None:
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO commerce_orders (
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, 'conn-1', ?, ?, 'delivered', 'paid', 'CNY', '100.00',
                      '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                      ?, 1, '2026-08-01T00:00:00+00:00',
                      '2026-08-01T00:00:00+00:00')
            """,
            (f"order-{order_id}", TENANT, STORE, order_id, "0" * 64),
        )


def seed_full_formal(service: ProfitService) -> None:
    register_default_policy(service)
    seed_delivered_order(service, "O1")
    service.record_entry(
        TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="O1")
    )
    service.record_entry(
        TENANT,
        entry(ExpenseCategory.REFUND_OFFSET, "-50.00", order_id="O1"),
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.PURCHASE_COST, "-300.00")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.DIRECT_PRODUCT_COST, "-100.00")
    )
    service.record_entry(TENANT, entry(ExpenseCategory.PLATFORM_FEE, "-80.00"))
    service.record_entry(
        TENANT, entry(ExpenseCategory.ADVERTISING_COST, "-50.00")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.FORWARD_LOGISTICS_COST, "-30.00")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.TRANSPORT_INSURANCE, "-10.00")
    )
    service.record_entry(TENANT, entry(ExpenseCategory.TAX_COST, "-20.00"))


def test_three_layers_from_single_ledger(tmp_path) -> None:
    service = make_service(tmp_path)
    seed_full_formal(service)
    view = service.projection(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert view.sales.status == "available"
    assert view.sales.amount == "550.00"
    assert view.operating.status == "available"
    assert view.operating.amount == "380.00"
    assert view.final.status == "available"
    assert view.final.amount == "360.00"
    assert view.final.label == "财务最终净利润"


def test_missing_required_category_blocks_layer_and_never_zero(tmp_path) -> None:
    service = make_service(tmp_path)
    register_default_policy(service)
    seed_delivered_order(service, "O1")
    service.record_entry(
        TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="O1")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.PURCHASE_COST, "-300.00")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.DIRECT_PRODUCT_COST, "-100.00")
    )
    view = service.projection(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert view.sales.status == "available"
    assert view.operating.status == "missing"
    assert "platform_fee" in view.operating.missing_fields
    assert view.final.status == "missing"
    assert view.operating.amount is None
    assert view.final.amount is None


def test_demo_scope_isolated_and_labeled(tmp_path) -> None:
    service = make_service(tmp_path)
    register_default_policy(service)
    service.record_entry(
        TENANT,
        entry(
            ExpenseCategory.SIGNED_REVENUE,
            "2000.00",
            order_id="O-DEMO",
            scope=ProfitScope.DEMO,
        ),
    )
    service.record_entry(
        TENANT,
        entry(
            ExpenseCategory.PURCHASE_COST,
            "-500.00",
            scope=ProfitScope.DEMO,
        ),
    )
    service.record_entry(
        TENANT,
        entry(
            ExpenseCategory.DIRECT_PRODUCT_COST,
            "-200.00",
            scope=ProfitScope.DEMO,
        ),
    )
    formal = service.projection(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    demo = service.projection(TENANT, STORE, PERIOD, ProfitScope.DEMO)
    assert formal.sales.status == "missing"
    assert "signed_receipt_revenue" in formal.sales.missing_fields
    assert demo.sales.status == "available"
    assert demo.sales.amount == "1300.00"
    assert demo.sales.label == "销售利润试算（演示参数）"
    assert demo.final.label == "净利润试算（演示参数）"


def test_signed_revenue_requires_order(tmp_path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service.record_entry(
            TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00")
        )
    assert "revenue_entry_requires_order" in str(exc.value)


def test_duplicate_entry_key_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    payload = entry(ExpenseCategory.PURCHASE_COST, "-100.00", entry_key="dup-1")
    service.record_entry(TENANT, payload)
    with pytest.raises(ProfitError) as exc:
        service.record_entry(TENANT, payload)
    assert "profit_ledger_entry_duplicate" in str(exc.value)


def test_double_count_refund_offset_detected(tmp_path) -> None:
    service = make_service(tmp_path)
    seed_delivered_order(service, "O1")
    service.record_entry(
        TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="O1")
    )
    service.record_entry(
        TENANT,
        entry(ExpenseCategory.REFUND_OFFSET, "-50.00", order_id="O1", entry_key="r1"),
    )
    service.record_entry(
        TENANT,
        entry(ExpenseCategory.REFUND_OFFSET, "-50.00", order_id="O1", entry_key="r2"),
    )
    result = service.reconcile(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert result.double_count_ok is False
    assert any(
        issue.code == "duplicate_order_category_entry" for issue in result.issues
    )


def test_refund_without_signed_revenue_flagged(tmp_path) -> None:
    service = make_service(tmp_path)
    seed_delivered_order(service, "NO-REV")
    service.record_entry(
        TENANT,
        entry(ExpenseCategory.REFUND_OFFSET, "-50.00", order_id="NO-REV"),
    )
    result = service.reconcile(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert any(
        issue.code == "refund_without_signed_revenue" for issue in result.issues
    )


def test_cross_period_refund_is_not_false_positive(tmp_path) -> None:
    service = make_service(tmp_path)
    seed_delivered_order(service, "O-CROSS")
    service.record_entry(
        TENANT,
        LedgerEntryInput(
            store_id=STORE,
            period="2026-07",
            category=ExpenseCategory.SIGNED_REVENUE,
            scope=ProfitScope.FORMAL,
            amount="1000.00",
            source_kind="actual",
            order_id="O-CROSS",
            entry_key="rev-cross",
        ),
    )
    service.record_entry(
        TENANT,
        entry(ExpenseCategory.REFUND_OFFSET, "-50.00", order_id="O-CROSS"),
    )
    result = service.reconcile(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert result.double_count_ok is True
    assert not any(
        issue.code == "refund_without_signed_revenue" for issue in result.issues
    )


def test_demo_source_cannot_enter_formal_scope(tmp_path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service.record_entry(
            TENANT,
            LedgerEntryInput(
                store_id=STORE,
                period=PERIOD,
                category=ExpenseCategory.PURCHASE_COST,
                scope=ProfitScope.FORMAL,
                amount="-100.00",
                source_kind="demo",
                entry_key="bad-scope",
            ),
        )
    assert "demo_source_cannot_enter_formal_scope" in str(exc.value)


def test_formal_revenue_requires_actual_source(tmp_path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service.record_entry(
            TENANT,
            LedgerEntryInput(
                store_id=STORE,
                period=PERIOD,
                category=ExpenseCategory.SIGNED_REVENUE,
                scope=ProfitScope.FORMAL,
                amount="1000.00",
                source_kind="manual",
                order_id="O1",
                entry_key="manual-revenue",
            ),
        )
    assert "formal_revenue_requires_actual_source" in str(exc.value)


def test_category_belongs_to_exactly_one_layer() -> None:
    seen: dict[ExpenseCategory, ProfitLayer] = {}
    for category, layer in CATEGORY_LAYER.items():
        assert category not in seen or seen[category] is layer
        seen[category] = layer
    assert len(seen) == len(list(ExpenseCategory))


def test_ghost_order_revenue_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ProfitError) as exc:
        service.record_entry(
            TENANT,
            entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="GHOST"),
        )
    assert "signed_receipt_required" in str(exc.value)


def test_non_delivered_order_revenue_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO commerce_orders (
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES ('order-ND', ?, 'conn-1', ?, 'ND', 'shipped', 'paid', 'CNY', '100.00',
                      '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                      ?, 1, '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')
            """,
            (TENANT, STORE, "0" * 64),
        )
    with pytest.raises(ProfitError) as exc:
        service.record_entry(
            TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="ND")
        )
    assert "signed_receipt_required" in str(exc.value)


def test_duplicate_ledger_entry_detected(tmp_path) -> None:
    service = make_service(tmp_path)
    service.record_entry(
        TENANT, entry(ExpenseCategory.PURCHASE_COST, "-300.00", entry_key="pc-a")
    )
    service.record_entry(
        TENANT, entry(ExpenseCategory.PURCHASE_COST, "-300.00", entry_key="pc-b")
    )
    result = service.reconcile(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert result.double_count_ok is False
    assert any(
        issue.code == "duplicate_ledger_entry" for issue in result.issues
    )


def test_positive_refund_and_negative_revenue_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service.record_entry(
            TENANT,
            entry(ExpenseCategory.REFUND_OFFSET, "50.00", order_id="O1"),
        )
    assert "ledger_amount_must_be_negative" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        service.record_entry(
            TENANT,
            entry(ExpenseCategory.SIGNED_REVENUE, "-1000.00", order_id="O1"),
        )
    assert "ledger_amount_must_be_positive" in str(exc.value)


def test_custom_policy_required_categories_respected(tmp_path) -> None:
    service = make_service(tmp_path)
    service.register_policy(
        TENANT,
        ProfitPolicyInput(
            policy_version="v-custom",
            required_categories={
                ProfitLayer.SALES: [ExpenseCategory.SIGNED_REVENUE],
                ProfitLayer.OPERATING: [
                    ExpenseCategory.SIGNED_REVENUE,
                    ExpenseCategory.PLATFORM_FEE,
                ],
                ProfitLayer.FINAL: [
                    ExpenseCategory.SIGNED_REVENUE,
                    ExpenseCategory.PLATFORM_FEE,
                    ExpenseCategory.TAX_COST,
                ],
            },
        ),
    )
    seed_delivered_order(service, "O1")
    service.record_entry(
        TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="O1")
    )
    service.record_entry(TENANT, entry(ExpenseCategory.PLATFORM_FEE, "-80.00"))
    service.record_entry(TENANT, entry(ExpenseCategory.TAX_COST, "-20.00"))
    view = service.projection(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert view.sales.status == "available"
    assert view.operating.status == "available"
    assert view.operating.amount == "920.00"
    assert view.final.status == "available"
    assert view.final.amount == "900.00"


def _entry_with_granularity(
    category: ExpenseCategory,
    amount: str,
    *,
    granularity: str | None,
    sku_id: str | None = None,
    entry_key: str,
) -> LedgerEntryInput:
    return LedgerEntryInput(
        store_id=STORE,
        period=PERIOD,
        category=category,
        scope=ProfitScope.FORMAL,
        amount=amount,
        source_kind="manual",
        granularity=granularity,
        sku_id=sku_id,
        entry_key=entry_key,
    )


def test_mixed_granularity_projection_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    register_default_policy(service)
    seed_delivered_order(service, "O1")
    service.record_entry(
        TENANT, entry(ExpenseCategory.SIGNED_REVENUE, "1000.00", order_id="O1")
    )
    service.record_entry(
        TENANT,
        _entry_with_granularity(
            ExpenseCategory.PLATFORM_FEE,
            "-80.00",
            granularity="store",
            entry_key="pf-store",
        ),
    )
    service.record_entry(
        TENANT,
        _entry_with_granularity(
            ExpenseCategory.PLATFORM_FEE,
            "-10.00",
            granularity="order",
            entry_key="pf-order",
        ),
    )
    with pytest.raises(ProfitError) as exc:
        service.projection(TENANT, STORE, PERIOD, ProfitScope.FORMAL)
    assert "mixed_granularity_projection" in str(exc.value)


def test_ledger_list_entries_filters_by_sku_and_period(tmp_path) -> None:
    service = make_service(tmp_path)
    register_default_policy(service)
    service.record_entry(
        TENANT,
        _entry_with_granularity(
            ExpenseCategory.ADVERTISING_COST,
            "-50.00",
            granularity="store",
            entry_key="ad-1",
        ),
    )
    service.record_entry(
        TENANT,
        _entry_with_granularity(
            ExpenseCategory.PURCHASE_COST,
            "-30.00",
            granularity="order",
            sku_id="SKU-1",
            entry_key="pc-1",
        ),
    )
    rows = service.list_entries(
        TENANT, STORE, sku_id="SKU-1", period=PERIOD, scope=ProfitScope.FORMAL
    )
    assert len(rows) == 1
    assert rows[0]["sku_id"] == "SKU-1"
    assert rows[0]["category"] == ExpenseCategory.PURCHASE_COST.value
    assert rows[0]["granularity"] == "order"
