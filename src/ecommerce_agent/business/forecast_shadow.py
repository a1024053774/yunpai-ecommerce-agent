from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .demand_facts import DemandFactService
from .forecast_backtest import compute_interval_coverage, compute_metrics, compute_pinball_loss
from .forecasting import ForecastingService


class ForecastShadowService:
    """Compare a stored forecast with later facts without mutating production state."""

    def __init__(self, *, demand_facts: DemandFactService, forecasting: ForecastingService):
        self.demand_facts = demand_facts
        self.forecasting = forecasting

    def evaluate(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        run = self.forecasting.get_run(tenant_id, run_id)
        training_end = date.fromisoformat(str(run["training_end"]))
        horizon = int(run["forecast_horizon"])
        end_date = training_end + timedelta(days=horizon)
        facts = self.demand_facts.list_facts(
            tenant_id,
            store_id=str(run["store_id"]),
            sku_id=str(run["sku_id"]),
            start_date=training_end + timedelta(days=1),
            end_date=end_date,
        )
        points = run["forecast_points"]
        if len(facts) < horizon:
            return {
                "run_id": run_id,
                "status": "pending",
                "training_end": training_end.isoformat(),
                "evaluation_end": end_date.isoformat(),
                "observed_days": len(facts),
                "required_days": horizon,
                "quality": "insufficient_future_facts",
                "persisted": False,
            }
        excluded = [
            item
            for item in facts[:horizon]
            if item["quality"].get("missing_source_date") or item["stockout_flag"] == "true"
        ]
        if excluded:
            return {
                "run_id": run_id,
                "status": "pending",
                "training_end": training_end.isoformat(),
                "evaluation_end": end_date.isoformat(),
                "observed_days": len(facts[:horizon]),
                "required_days": horizon,
                "quality": "future_facts_degraded",
                "excluded_days": [item["business_date"] for item in excluded],
                "persisted": False,
            }
        actual = [Decimal(str(item["eligible_units"])) for item in facts[:horizon]]
        p50 = [Decimal(str(point["p50"])) for point in points[:horizon]]
        forecast = [Decimal(str(point["p50"])) for point in points[:horizon]]
        p80 = [Decimal(str(point["p80"])) for point in points[:horizon]]
        p95 = [Decimal(str(point["p95"])) for point in points[:horizon]]
        return {
            "run_id": run_id,
            "status": "evaluated",
            "training_end": training_end.isoformat(),
            "evaluation_end": end_date.isoformat(),
            "observed_days": len(actual),
            "required_days": horizon,
            "metrics": {
                key: str(value) if value is not None else None
                for key, value in compute_metrics(actual, forecast).items()
            },
            "pinball_loss": {
                "p50": str(compute_pinball_loss(actual, p50, Decimal("0.5"))),
                "p80": str(compute_pinball_loss(actual, p80, Decimal("0.8"))),
                "p95": str(compute_pinball_loss(actual, p95, Decimal("0.95"))),
            },
            "interval_coverage": {
                "p80": str(
                    compute_interval_coverage(
                        actual,
                        lower=[Decimal("0")] * horizon,
                        upper=p80,
                    )
                ),
                "p95": str(
                    compute_interval_coverage(
                        actual,
                        lower=[Decimal("0")] * horizon,
                        upper=p95,
                    )
                ),
            },
            "data_hash": run["data_hash"],
            "persisted": False,
        }
