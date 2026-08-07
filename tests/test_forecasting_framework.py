from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import InventoryBalanceUpsert, OrderUpsert
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.business.forecasting import (
    ForecastRequest,
    LastValueModel,
    SevenDaySeasonalNaiveModel,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
TENANT = "tenant-test"
STORE = "store-001"
WAREHOUSE = "warehouse-001"
SKU = "SKU-001"
ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


def _source_time(business_day: date, hour: int = 12) -> datetime:
    return datetime.combine(business_day, time(hour), tzinfo=SHANGHAI).astimezone(UTC)


def _order(
    order_id: str,
    business_day: date,
    *,
    quantity: int,
    order_status: str = "paid",
    payment_status: str = "paid",
    store_id: str = STORE,
    sku_id: str = SKU,
    hour: int = 12,
) -> OrderUpsert:
    placed_at = _source_time(business_day, hour)
    return OrderUpsert(
        connector_id="test-connector",
        store_id=store_id,
        order_id=order_id,
        order_status=order_status,
        payment_status=payment_status,
        currency="CNY",
        total_amount=Decimal("10.00") * quantity,
        placed_at=placed_at,
        lines=[
            OrderLineInput(
                line_id=f"line-{order_id}",
                sku_id=sku_id,
                title="测试商品",
                quantity=quantity,
                unit_price=Decimal("10.00"),
            )
        ],
        source_updated_at=placed_at,
        source_id=f"source-{order_id}",
    )


def _inventory(
    *,
    tenant_id: str = TENANT,
    store_id: str = STORE,
    warehouse_id: str = WAREHOUSE,
    sku_id: str = SKU,
    on_hand: str = "10",
    reserved: str = "2",
    inbound: str = "3",
) -> InventoryBalanceUpsert:
    now = datetime.now(UTC)
    return InventoryBalanceUpsert(
        connector_id="test-connector",
        store_id=store_id,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        on_hand=Decimal(on_hand),
        reserved=Decimal(reserved),
        inbound=Decimal(inbound),
        source_updated_at=now,
        source_id="inventory-source",
    )


def _seed_weekly_history(service: AgentService, *, start: date, quantities: list[int]) -> None:
    for index, quantity in enumerate(quantities):
        if quantity == 0:
            continue
        day = start + timedelta(days=index)
        service.operations.orders.upsert(
            TENANT,
            _order(f"order-{index}", day, quantity=quantity),
        )


def test_demand_series_filters_order_status_and_uses_shanghai_business_date(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.operations.orders.upsert(
            TENANT,
            _order("eligible", date(2026, 8, 1), quantity=2, hour=23),
        )
        service.operations.orders.upsert(
            TENANT,
            _order("cancelled", date(2026, 8, 1), quantity=5, order_status="canceled"),
        )
        service.operations.orders.upsert(
            TENANT,
            _order("unpaid", date(2026, 8, 1), quantity=7, payment_status="unpaid"),
        )

        series = service.operations.forecasting.build_demand_series(
            TENANT, store_id=STORE, sku_id=SKU, history_days=14
        )

        assert series.policy_version == "demand-v1"
        assert series.timezone == "Asia/Shanghai"
        assert series.total_eligible_units == Decimal("2")
        assert series.points[0].business_date == date(2026, 8, 1)
        assert series.points[0].eligible_units == Decimal("2")
        assert series.points[0].gross_units == Decimal("14")

        # 16:30 UTC is the next business date in Asia/Shanghai.
        service.operations.orders.upsert(
            TENANT,
            _order("boundary", date(2026, 8, 2), quantity=3, hour=0),
        )
        boundary = service.operations.forecasting.build_demand_series(
            TENANT, store_id=STORE, sku_id=SKU, history_days=14
        )
        assert boundary.points[-1].business_date == date(2026, 8, 2)
        assert boundary.points[-1].eligible_units == Decimal("3")
    finally:
        service.close()


def test_demand_series_fills_missing_days_and_rejects_cold_start(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[1, 0, 3, 0, 2, 0, 4, 1, 0, 3, 0, 2, 0, 4],
        )
        series = service.operations.forecasting.build_demand_series(
            TENANT, store_id=STORE, sku_id=SKU, history_days=56
        )
        assert len(series.points) == 14
        assert series.missing_business_dates == [
            date(2026, 7, 2),
            date(2026, 7, 4),
            date(2026, 7, 6),
            date(2026, 7, 9),
            date(2026, 7, 11),
            date(2026, 7, 13),
        ]
        assert series.points[1].eligible_units == Decimal("0")

        with pytest.raises(ValueError, match="forecast_insufficient_history"):
            service.operations.forecasting.preview(
                TENANT,
                ForecastRequest(
                    store_id=STORE,
                    warehouse_id=WAREHOUSE,
                    sku_id="COLD-SKU",
                    horizon_days=7,
                ),
            )
    finally:
        service.close()


def test_models_are_deterministic_and_forecast_quantiles_are_monotonic(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[1, 2, 3, 4, 5, 6, 7] * 2,
        )
        service.operations.inventory.upsert(TENANT, _inventory())
        request = ForecastRequest(
            store_id=STORE,
            warehouse_id=WAREHOUSE,
            sku_id=SKU,
            horizon_days=14,
        )
        first = service.operations.forecasting.preview(TENANT, request)
        second = service.operations.forecasting.preview(TENANT, request)

        assert first["model"]["name"] == "7_day_seasonal_naive"
        assert first["forecast_points"] == second["forecast_points"]
        assert len(first["forecast_points"]) == 14
        assert all(
            Decimal(item["p50"]) <= Decimal(item["p80"]) <= Decimal(item["p95"])
            for item in first["forecast_points"]
        )
        assert first["status"] == "draft"
        assert first["persisted"] is False
        assert first["external_order_created"] is False
        with service.db.connect() as conn:
            forecast_tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forecast%'"
            ).fetchall()
        assert forecast_tables == []
    finally:
        service.close()


def test_baseline_models_have_explicit_deterministic_contract() -> None:
    last_value = LastValueModel()
    seasonal = SevenDaySeasonalNaiveModel()
    assert last_value.predict([Decimal("2"), Decimal("5")], 3) == [
        Decimal("5"),
        Decimal("5"),
        Decimal("5"),
    ]
    assert seasonal.predict([Decimal(str(index)) for index in range(14)], 9) == [
        Decimal("7"),
        Decimal("8"),
        Decimal("9"),
        Decimal("10"),
        Decimal("11"),
        Decimal("12"),
        Decimal("13"),
        Decimal("7"),
        Decimal("8"),
    ]


@pytest.mark.parametrize("horizon", [7, 14, 30])
def test_forecasting_supports_declared_horizons(tmp_path, horizon: int) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[2] * 14,
        )
        service.operations.inventory.upsert(TENANT, _inventory())
        result = service.operations.forecasting.preview(
            TENANT,
            ForecastRequest(
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                horizon_days=horizon,
            ),
        )
        assert result["policy"]["horizon_days"] == horizon
        assert len(result["forecast_points"]) >= horizon
    finally:
        service.close()


def test_replenishment_formula_applies_supply_safety_stock_and_order_multiple(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[10] * 14,
        )
        service.operations.inventory.upsert(
            TENANT,
            _inventory(on_hand="20", reserved="4", inbound="3"),
        )
        result = service.operations.forecasting.preview(
            TENANT,
            ForecastRequest(
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                horizon_days=7,
                lead_time_days=7,
                review_period_days=7,
                safety_stock_days=3,
                minimum_order_qty=20,
                order_multiple=10,
            ),
        )

        assert result["inventory"]["available"] == "16.00"
        assert result["inventory"]["inbound"] == "3.00"
        assert result["replenishment"]["recommended_order_qty"] == "160.00"
        assert result["replenishment"]["rounding"]["minimum_order_qty"] == "20.00"
        assert result["replenishment"]["rounding"]["order_multiple"] == "10.00"
    finally:
        service.close()


def test_preview_returns_reusable_forecast_order_draft(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[10] * 14,
        )
        service.operations.inventory.upsert(
            TENANT,
            _inventory(on_hand="20", reserved="0", inbound="0"),
        )

        result = service.operations.forecasting.preview(
            TENANT,
            ForecastRequest(
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                horizon_days=7,
                lead_time_days=3,
                review_period_days=7,
                service_level="p80",
            ),
        )

        draft = result["forecast_order"]
        assert draft["kind"] == "forecast_replenishment"
        assert draft["status"] == "draft"
        assert draft["persisted"] is False
        assert draft["external_order_created"] is False
        assert draft["store_id"] == STORE
        assert draft["warehouse_id"] == WAREHOUSE
        assert draft["sku_id"] == SKU
        assert draft["recommended_quantity"] == result["replenishment"]["recommended_order_qty"]
        assert draft["expected_stockout_date"] == "2026-07-17"
        assert draft["recommended_arrival_date"] == "2026-07-17"
        assert draft["service_level"] == "p80"
        assert draft["forecast_basis"] == {
            "model": "7_day_seasonal_naive",
            "data_watermark": result["data_quality"]["source_watermark"],
            "demand_policy_version": "demand-v1",
        }
    finally:
        service.close()


def test_forecasting_is_tenant_and_warehouse_isolated(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[1] * 14,
        )
        service.operations.inventory.upsert(TENANT, _inventory())
        with pytest.raises(ValueError, match="forecast_insufficient_history"):
            service.operations.forecasting.preview(
                "tenant-other",
                ForecastRequest(
                    store_id=STORE,
                    warehouse_id=WAREHOUSE,
                    sku_id=SKU,
                ),
            )
        with pytest.raises(ValueError, match="inventory_balance_not_found"):
            service.operations.forecasting.preview(
                TENANT,
                ForecastRequest(
                    store_id=STORE,
                    warehouse_id="warehouse-other",
                    sku_id=SKU,
                ),
            )
    finally:
        service.close()


def test_forecasting_preview_api_requires_admin_and_returns_draft(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        _seed_weekly_history(
            service,
            start=date(2026, 7, 1),
            quantities=[2] * 14,
        )
        service.operations.inventory.upsert(TENANT, _inventory())
        with service.db.connect() as conn:
            before_order_count = conn.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0]
        with TestClient(app) as client:
            unauthorized = client.post(
                "/v1/forecasting/preview",
                json={"store_id": STORE, "warehouse_id": WAREHOUSE, "sku_id": SKU},
            )
            assert unauthorized.status_code in (401, 503)

            response = client.post(
                "/v1/forecasting/preview",
                headers=ADMIN_HEADERS,
                json={"store_id": STORE, "warehouse_id": WAREHOUSE, "sku_id": SKU},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "draft"
            assert body["persisted"] is False
            assert body["external_order_created"] is False
            assert body["forecast_order"]["kind"] == "forecast_replenishment"
            assert body["forecast_order"]["status"] == "draft"
            assert body["forecast_order"]["persisted"] is False
            assert body["forecast_order"]["external_order_created"] is False
            assert body["forecast_order"]["sku_id"] == SKU

            audit = client.get(
                "/v1/admin/audit?event_type=forecasting.preview.generated",
                headers=ADMIN_HEADERS,
            )
            assert audit.status_code == 200
            assert audit.json()[0]["detail"]["sku_id"] == SKU
        with service.db.connect() as conn:
            after_order_count = conn.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0]
            forecast_plan_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory_plans'"
            ).fetchone()
        assert after_order_count == before_order_count
        assert forecast_plan_table is None
    finally:
        service.close()
