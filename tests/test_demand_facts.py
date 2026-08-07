from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from ecommerce_agent.business import OrderUpsert
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.service import AgentService

from conftest import make_settings


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
TENANT = "tenant-demand"
STORE = "store-demand"
SKU = "sku-demand"


def _order(order_id: str, day: date, *, quantity: int, status: str = "paid") -> OrderUpsert:
    placed_at = datetime.combine(day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
    return OrderUpsert(
        connector_id="demand-connector",
        store_id=STORE,
        order_id=order_id,
        order_status=status,
        payment_status="paid" if status != "canceled" else "unpaid",
        currency="CNY",
        total_amount=Decimal(quantity * 10),
        placed_at=placed_at,
        lines=[
            OrderLineInput(
                line_id=f"line-{order_id}",
                sku_id=SKU,
                title="Demand fixture",
                quantity=quantity,
                unit_price=Decimal("10"),
            )
        ],
        source_updated_at=placed_at,
        source_id=f"source-{order_id}",
    )


def test_rebuild_persists_daily_facts_and_replays_idempotently(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.operations.orders.upsert(TENANT, _order("eligible", date(2026, 8, 1), quantity=2))
        service.operations.orders.upsert(
            TENANT, _order("cancelled", date(2026, 8, 1), quantity=9, status="canceled")
        )
        service.operations.orders.upsert(TENANT, _order("later", date(2026, 8, 3), quantity=4))

        first = service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
        )
        second = service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
        )

        assert first["fact_version"] == 1
        assert first["write_status"] == "applied"
        assert second["write_status"] == "idempotent"
        facts = service.operations.demand_facts.list_facts(
            TENANT, store_id=STORE, sku_id=SKU
        )
        assert [(item["business_date"], item["eligible_units"]) for item in facts] == [
            ("2026-08-01", "2.00"),
            ("2026-08-02", "0.00"),
            ("2026-08-03", "4.00"),
        ]
        assert facts[0]["gross_units"] == "11.00"
        assert facts[1]["quality"]["missing_source_date"] is True
        assert facts[0]["stockout_flag"] == "unknown"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM demand_daily_facts").fetchone()[0] == 3
    finally:
        service.close()


def test_later_order_correction_appends_a_new_fact_version(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        original = _order("corrected", date(2026, 8, 1), quantity=2)
        service.operations.orders.upsert(TENANT, original)
        first = service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
        )

        corrected = original.model_copy(
            update={
                "lines": [
                    OrderLineInput(
                        line_id="line-corrected",
                        sku_id=SKU,
                        title="Demand fixture",
                        quantity=5,
                        unit_price=Decimal("10"),
                    )
                ],
                "total_amount": Decimal("50"),
                "source_updated_at": original.source_updated_at + timedelta(hours=1),
            }
        )
        service.operations.orders.upsert(TENANT, corrected)
        second = service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
        )

        assert first["fact_version"] == 1
        assert second["fact_version"] == 2
        assert second["facts"][0]["eligible_units"] == "5.00"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM demand_daily_facts").fetchone()[0] == 2
    finally:
        service.close()


def test_demand_facts_are_tenant_scoped(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.operations.orders.upsert(TENANT, _order("tenant-order", date(2026, 8, 1), quantity=1))
        with pytest.raises(ValueError, match="demand_no_source_data"):
            service.operations.demand_facts.rebuild(
                "tenant-other",
                store_id=STORE,
                sku_id=SKU,
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
            )
    finally:
        service.close()
