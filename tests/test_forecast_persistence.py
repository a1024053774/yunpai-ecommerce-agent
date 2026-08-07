from __future__ import annotations

from datetime import date, time, timedelta, timezone, datetime
from decimal import Decimal

from ecommerce_agent.business import OrderUpsert
from ecommerce_agent.business.forecasting import ForecastRunRequest
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.service import AgentService

from conftest import make_settings


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
TENANT = "tenant-run"
STORE = "store-run"
SKU = "sku-run"


def _seed_orders(service: AgentService) -> None:
    for index in range(35):
        day = date(2026, 7, 1) + timedelta(days=index)
        placed_at = datetime.combine(day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        service.operations.orders.upsert(
            TENANT,
            OrderUpsert(
                connector_id="run-connector",
                store_id=STORE,
                order_id=f"run-order-{index}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal("20"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"run-line-{index}",
                        sku_id=SKU,
                        title="Run fixture",
                        quantity=(index % 7) + 1,
                        unit_price=Decimal("20"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"run-source-{index}",
            ),
        )


def test_forecast_run_persists_backtests_points_and_champion(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _seed_orders(service)
        service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 4),
        )

        result = service.operations.forecasting.run(
            TENANT,
            ForecastRunRequest(
                store_id=STORE,
                sku_id=SKU,
                horizon_days=7,
                backtest_windows=3,
            ),
        )

        assert result["status"] == "succeeded"
        assert result["run_id"]
        assert result["champion_model"]
        assert result["champion_reason"]
        assert result["backtest_summary"]
        assert result["backtest_summary"][0]["model"] == result["champion_model"]
        assert len(result["forecast_points"]) == 7
        assert all(
            Decimal(point["p50"]) <= Decimal(point["p80"]) <= Decimal(point["p95"])
            for point in result["forecast_points"]
        )
        assert result["data_quality"]["demand_policy_version"] == "demand-v1"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM forecast_backtests").fetchone()[0] > 0
            assert conn.execute("SELECT COUNT(*) FROM forecast_points").fetchone()[0] == 7
            run_row = conn.execute(
                "SELECT training_end, data_hash FROM forecast_runs WHERE run_id=?",
                (result["run_id"],),
            ).fetchone()
        assert run_row["training_end"] == "2026-08-04"
        assert run_row["data_hash"]
    finally:
        service.close()
