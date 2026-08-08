from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ecommerce_agent.business.forecasting import ForecastRunRequest
from ecommerce_agent.business.orders import OrderLineInput, OrderUpsert
from ecommerce_agent.config import Settings
from ecommerce_agent.service import AgentService


TENANT_ID = "local-appliance"
STORE_ID = "virtual-forecast-store"
SKU_ID = "virtual-forecast-sku"
START_DATE = date(2023, 8, 9)
SALES_DAY_COUNT = 1095
BACKTEST_HORIZON_DAYS = 30
BACKTEST_WINDOW_COUNT = 3
SHANGHAI = timezone(timedelta(hours=8))


def virtual_daily_units(day: date, index: int) -> int:
    """Return a deterministic, explicitly virtual daily sales quantity."""
    weekday_pattern = (9, 11, 12, 13, 16, 22, 18)
    annual_adjustment = 4 if day.month in {11, 12} else 2 if day.month in {6, 7} else 0
    trend = index // 180
    promotion = 9 if index % 113 == 0 else 0
    return weekday_pattern[day.weekday()] + annual_adjustment + trend + promotion


def run_three_year_demo(service: AgentService) -> dict[str, Any]:
    end_date = START_DATE + timedelta(days=SALES_DAY_COUNT - 1)
    for index in range(SALES_DAY_COUNT):
        business_day = START_DATE + timedelta(days=index)
        placed_at = datetime.combine(business_day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        quantity = virtual_daily_units(business_day, index)
        service.operations.orders.upsert(
            TENANT_ID,
            OrderUpsert(
                connector_id="virtual-forecast-evaluation",
                store_id=STORE_ID,
                order_id=f"virtual-forecast-order-{business_day.isoformat()}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal(quantity) * Decimal("99"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"virtual-forecast-line-{business_day.isoformat()}",
                        sku_id=SKU_ID,
                        title="虚拟三年预测验收商品",
                        quantity=quantity,
                        unit_price=Decimal("99"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"virtual-forecast-source-{business_day.isoformat()}",
            ),
        )

    service.operations.demand_facts.rebuild(
        TENANT_ID,
        store_id=STORE_ID,
        sku_id=SKU_ID,
        start_date=START_DATE,
        end_date=end_date,
        stockout_statuses={
            START_DATE + timedelta(days=index): "false"
            for index in range(SALES_DAY_COUNT)
        },
    )
    run = service.operations.forecasting.run(
        TENANT_ID,
        ForecastRunRequest(
            store_id=STORE_ID,
            sku_id=SKU_ID,
            horizon_days=BACKTEST_HORIZON_DAYS,
            start_date=START_DATE,
            end_date=end_date,
            minimum_history_days=365,
            backtest_windows=BACKTEST_WINDOW_COUNT,
            backtest_step_days=BACKTEST_HORIZON_DAYS,
        ),
    )
    comparison = _actual_vs_forecast(run)
    return {
        "virtual": True,
        "production_claim": False,
        "tenant_id": TENANT_ID,
        "store_id": STORE_ID,
        "sku_id": SKU_ID,
        "sales_day_count": SALES_DAY_COUNT,
        "training_start": run["training_start"],
        "training_end": run["training_end"],
        "run_id": run["run_id"],
        "champion_model": run["champion_model"],
        "metrics": run["metrics"],
        "comparison_day_count": len(comparison),
        "actual_vs_forecast": comparison,
        "future_forecast": run["forecast_points"],
    }


def _actual_vs_forecast(run: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in run["backtests"]:
        if record["model_name"] != run["champion_model"]:
            continue
        start = date.fromisoformat(record["origin_date"]) + timedelta(days=1)
        for index, actual in enumerate(record["actual"]):
            rows.append(
                {
                    "date": (start + timedelta(days=index)).isoformat(),
                    "actual": str(actual),
                    "forecast": str(record["forecast"][index]),
                }
            )
    return sorted(rows, key=lambda item: item["date"])


def demo_settings(data_dir: Path) -> Settings:
    return replace(
        Settings.from_env(),
        data_dir=data_dir.resolve(),
        model_enabled=False,
        outbox_worker_enabled=False,
        channel_agent_worker_enabled=False,
        competitive_monitor_worker_enabled=False,
        handoff_sla_worker_enabled=False,
        handoff_dispatch_worker_enabled=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行三年虚拟销售预测评估")
    parser.add_argument("--data-dir", type=Path, required=True, help="用于保存虚拟验收数据的目录")
    args = parser.parse_args()
    service = AgentService(demo_settings(args.data_dir))
    try:
        print(json.dumps(run_three_year_demo(service), ensure_ascii=False, indent=2))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
