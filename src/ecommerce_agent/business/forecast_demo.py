from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from .forecasting import ForecastRunRequest
from .inventory import InventoryBalanceUpsert
from .inventory_planning import InventoryPlanningPolicy
from .orders import OrderLineInput, OrderUpsert


DEMO_STORE_ID = "__forecast_demo_store__"
DEMO_SKU_ID = "__forecast_demo_sku__"
DEMO_WAREHOUSE_ID = "__forecast_demo_warehouse__"
START_DATE = date(2023, 8, 9)
SALES_DAY_COUNT = 1095
SHANGHAI = timezone(timedelta(hours=8))
DEMO_SOURCE_UPDATED_AT = datetime(2026, 8, 8, 4, tzinfo=UTC)


def virtual_daily_units(day: date, index: int) -> int:
    weekday_pattern = (9, 11, 12, 13, 16, 22, 18)
    annual_adjustment = 4 if day.month in {11, 12} else 2 if day.month in {6, 7} else 0
    trend = index // 180
    promotion = 9 if index % 113 == 0 else 0
    return weekday_pattern[day.weekday()] + annual_adjustment + trend + promotion


def ensure_three_year_demo(
    service: Any,
    *,
    tenant_id: str,
    horizon_days: int,
) -> dict[str, Any]:
    demo = ensure_three_year_demo_data(service, tenant_id=tenant_id)
    existing = service.operations.forecasting.latest_run(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
    )
    if existing is not None and int(existing["forecast_horizon"]) == horizon_days:
        return {**demo, "run": existing}

    end_date = START_DATE + timedelta(days=SALES_DAY_COUNT - 1)
    run = service.operations.forecasting.run(
        tenant_id,
        ForecastRunRequest(
            store_id=DEMO_STORE_ID,
            sku_id=DEMO_SKU_ID,
            horizon_days=horizon_days,
            start_date=START_DATE,
            end_date=end_date,
            minimum_history_days=365,
            backtest_windows=3,
            backtest_step_days=horizon_days,
        ),
    )
    return {**demo, "run": run}


def ensure_three_year_demo_data(service: Any, *, tenant_id: str) -> dict[str, Any]:
    existing = service.operations.demand_facts.list_response(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
        start_date=START_DATE,
        end_date=START_DATE + timedelta(days=SALES_DAY_COUNT - 1),
    )
    if len(existing["facts"]) == SALES_DAY_COUNT:
        return _source_view(tenant_id=tenant_id)

    end_date = START_DATE + timedelta(days=SALES_DAY_COUNT - 1)
    for index in range(SALES_DAY_COUNT):
        business_day = START_DATE + timedelta(days=index)
        placed_at = datetime.combine(business_day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        quantity = virtual_daily_units(business_day, index)
        service.operations.orders.upsert(
            tenant_id,
            OrderUpsert(
                connector_id="virtual-forecast-demo",
                store_id=DEMO_STORE_ID,
                order_id=f"virtual-forecast-order-{business_day.isoformat()}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal(quantity) * Decimal("99"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"virtual-forecast-line-{business_day.isoformat()}",
                        sku_id=DEMO_SKU_ID,
                        title="本地演示预测商品",
                        quantity=quantity,
                        unit_price=Decimal("99"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"virtual-forecast-source-{business_day.isoformat()}",
            ),
        )

    service.operations.demand_facts.rebuild(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
        start_date=START_DATE,
        end_date=end_date,
        stockout_statuses={
            START_DATE + timedelta(days=index): "false" for index in range(SALES_DAY_COUNT)
        },
    )
    return _source_view(tenant_id=tenant_id)


def ensure_three_year_demo_plan(
    service: Any,
    *,
    tenant_id: str,
    horizon_days: int,
) -> dict[str, Any]:
    demo = ensure_three_year_demo(
        service,
        tenant_id=tenant_id,
        horizon_days=horizon_days,
    )
    balances = service.operations.inventory.list_balances(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
    )
    balance = next(
        (item for item in balances if item["warehouse_id"] == DEMO_WAREHOUSE_ID),
        None,
    )
    if balance is None:
        balance = service.operations.inventory.upsert(
            tenant_id,
            InventoryBalanceUpsert(
                connector_id="virtual-forecast-demo",
                store_id=DEMO_STORE_ID,
                warehouse_id=DEMO_WAREHOUSE_ID,
                sku_id=DEMO_SKU_ID,
                on_hand=Decimal("54"),
                reserved=Decimal("6"),
                inbound=Decimal("0"),
                average_daily_sales=Decimal("18"),
                source_updated_at=DEMO_SOURCE_UPDATED_AT,
                source_id="virtual-forecast-demo-inventory-v1",
            ),
        )
    policy = service.operations.inventory_planning.latest_policy(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
        warehouse_id=DEMO_WAREHOUSE_ID,
    )
    if policy is None:
        policy = service.operations.inventory_planning.upsert_policy(
            tenant_id,
            InventoryPlanningPolicy(
                store_id=DEMO_STORE_ID,
                sku_id=DEMO_SKU_ID,
                warehouse_id=DEMO_WAREHOUSE_ID,
                supplier_lead_days=7,
                review_period_days=7,
                service_level="p80",
                minimum_order_qty=Decimal("24"),
                order_multiple=Decimal("12"),
                minimum_safety_stock=Decimal("20"),
                maximum_stock_days=90,
            ),
        )
    plan = service.operations.inventory_planning.latest_plan(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
        warehouse_id=DEMO_WAREHOUSE_ID,
    )
    if plan is None or plan["forecast_run_id"] != demo["run"]["run_id"]:
        plan = service.operations.inventory_planning.create_plan(
            tenant_id,
            forecast_run_id=demo["run"]["run_id"],
            warehouse_id=DEMO_WAREHOUSE_ID,
        )
    return {
        **demo,
        "warehouse_id": DEMO_WAREHOUSE_ID,
        "inventory": balance,
        "policy": policy,
        "plan": plan,
    }


def _source_view(*, tenant_id: str) -> dict[str, Any]:
    return {
        "virtual": True,
        "production_claim": False,
        "tenant_id": tenant_id,
        "store_id": DEMO_STORE_ID,
        "sku_id": DEMO_SKU_ID,
        "sales_day_count": SALES_DAY_COUNT,
    }
