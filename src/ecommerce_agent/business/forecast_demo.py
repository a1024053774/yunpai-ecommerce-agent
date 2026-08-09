from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from .forecasting import ForecastRunRequest
from .orders import OrderLineInput, OrderUpsert


DEMO_STORE_ID = "__forecast_demo_store__"
DEMO_SKU_ID = "__forecast_demo_sku__"
START_DATE = date(2023, 8, 9)
SALES_DAY_COUNT = 1095
SHANGHAI = timezone(timedelta(hours=8))


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
    existing = service.operations.forecasting.latest_run(
        tenant_id,
        store_id=DEMO_STORE_ID,
        sku_id=DEMO_SKU_ID,
    )
    if existing is not None and int(existing["forecast_horizon"]) == horizon_days:
        return _view(existing, tenant_id=tenant_id)

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
    return _view(run, tenant_id=tenant_id)


def _view(run: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    return {
        "virtual": True,
        "production_claim": False,
        "tenant_id": tenant_id,
        "store_id": DEMO_STORE_ID,
        "sku_id": DEMO_SKU_ID,
        "sales_day_count": SALES_DAY_COUNT,
        "run": run,
    }
