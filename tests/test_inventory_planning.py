from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from ecommerce_agent.business import InventoryBalanceUpsert, OrderUpsert
from ecommerce_agent.business.forecasting import ForecastRunRequest
from ecommerce_agent.business.inventory_planning import InventoryPlanningPolicy
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.service import AgentService

from conftest import make_settings


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
TENANT = "tenant-plan"
STORE = "store-plan"
SKU = "sku-plan"
WAREHOUSE = "warehouse-plan"


def _seed_and_run(service: AgentService) -> str:
    for index in range(35):
        day = date(2026, 7, 1) + timedelta(days=index)
        placed_at = datetime.combine(day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        service.operations.orders.upsert(
            TENANT,
            OrderUpsert(
                connector_id="plan-connector",
                store_id=STORE,
                order_id=f"plan-order-{index}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal("20"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"plan-line-{index}",
                        sku_id=SKU,
                        title="Plan fixture",
                        quantity=10,
                        unit_price=Decimal("20"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"plan-source-{index}",
            ),
        )
    service.operations.demand_facts.rebuild(
        TENANT,
        store_id=STORE,
        sku_id=SKU,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 8, 4),
    )
    return service.operations.forecasting.run(
        TENANT,
        ForecastRunRequest(store_id=STORE, sku_id=SKU, horizon_days=7),
    )["run_id"]


def test_inventory_plan_is_deterministic_and_preserves_calculation_evidence(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        run_id = _seed_and_run(service)
        service.operations.inventory.upsert(
            TENANT,
            InventoryBalanceUpsert(
                connector_id="plan-inventory",
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                on_hand=Decimal("30"),
                reserved=Decimal("5"),
                inbound=Decimal("10"),
                source_updated_at=datetime.now(UTC),
                source_id="plan-inventory-source",
            ),
        )
        policy = InventoryPlanningPolicy(
            store_id=STORE,
            sku_id=SKU,
            warehouse_id=WAREHOUSE,
            supplier_lead_days=3,
            review_period_days=2,
            service_level="p80",
            minimum_order_qty=Decimal("20"),
            order_multiple=Decimal("10"),
            minimum_safety_stock=Decimal("5"),
            maximum_stock_days=30,
        )
        service.operations.inventory_planning.upsert_policy(TENANT, policy)

        first = service.operations.inventory_planning.create_plan(
            TENANT, forecast_run_id=run_id, warehouse_id=WAREHOUSE
        )
        second = service.operations.inventory_planning.create_plan(
            TENANT, forecast_run_id=run_id, warehouse_id=WAREHOUSE
        )

        assert first["status"] == "draft"
        assert first["recommended_order_qty"] == second["recommended_order_qty"]
        assert first["calculation"]["available"] == "25.00"
        assert first["calculation"]["inbound"] == "10.00"
        assert first["calculation"]["minimum_safety_stock"] == "5.00"
        assert first["rounding"]["minimum_order_qty"] == "20.00"
        assert first["rounding"]["order_multiple"] == "10.00"
        assert first["explanation"]["warehouse_scope"] == "supply_location_only"
        assert set(first["expected_stockout_dates"]) == {"p50", "p80", "p95"}
        assert first["replenishment_order"]["status"] == "draft"
        assert first["replenishment_order"]["external_order_created"] is False
        assert first["explanation"]["forecast_data_quality"]
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM inventory_plans").fetchone()[0] == 2
    finally:
        service.close()


def test_inventory_plan_is_tenant_scoped(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        with pytest.raises(ValueError, match="forecast_run_not_found"):
            service.operations.inventory_planning.create_plan(
                "tenant-other", forecast_run_id="run-other", warehouse_id=WAREHOUSE
            )
    finally:
        service.close()


def test_inventory_plan_applies_maximum_stock_after_moq_and_multiple(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        run_id = _seed_and_run(service)
        service.operations.inventory.upsert(
            TENANT,
            InventoryBalanceUpsert(
                connector_id="plan-cap-inventory",
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                on_hand=Decimal("0"),
                reserved=Decimal("0"),
                inbound=Decimal("0"),
                source_updated_at=datetime.now(UTC),
                source_id="plan-cap-source",
            ),
        )
        service.operations.inventory_planning.upsert_policy(
            TENANT,
            InventoryPlanningPolicy(
                store_id=STORE,
                sku_id=SKU,
                warehouse_id=WAREHOUSE,
                supplier_lead_days=3,
                review_period_days=2,
                minimum_order_qty=Decimal("65"),
                order_multiple=Decimal("20"),
                minimum_safety_stock=Decimal("60"),
                maximum_stock_days=7,
            ),
        )

        plan = service.operations.inventory_planning.create_plan(
            TENANT, forecast_run_id=run_id, warehouse_id=WAREHOUSE
        )

        assert plan["recommended_order_qty"] == "70.00"
        assert plan["rounding"]["raw_order_qty"] == "110.00"
        assert plan["rounding"]["after_order_multiple"] == "120.00"
        assert plan["rounding"]["maximum_stock_limit"] == "70.00"
        assert plan["rounding"]["maximum_stock_cap_applied"] is True
    finally:
        service.close()


def test_inventory_plan_exposes_overstock_risk(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        run_id = _seed_and_run(service)
        service.operations.inventory.upsert(
            TENANT,
            InventoryBalanceUpsert(
                connector_id="plan-overstock-inventory",
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                on_hand=Decimal("100"),
                reserved=Decimal("0"),
                inbound=Decimal("0"),
                source_updated_at=datetime.now(UTC),
                source_id="plan-overstock-source",
            ),
        )
        service.operations.inventory_planning.upsert_policy(
            TENANT,
            InventoryPlanningPolicy(
                store_id=STORE,
                sku_id=SKU,
                warehouse_id=WAREHOUSE,
                supplier_lead_days=3,
                review_period_days=2,
                maximum_stock_days=5,
            ),
        )

        plan = service.operations.inventory_planning.create_plan(
            TENANT, forecast_run_id=run_id, warehouse_id=WAREHOUSE
        )

        assert plan["risk_level"] == "overstock"
    finally:
        service.close()
