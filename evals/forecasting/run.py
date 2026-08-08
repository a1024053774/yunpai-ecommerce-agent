from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ecommerce_agent.business.forecast_backtest import (
    ChampionSelector,
    RollingBacktest,
    compute_interval_coverage,
    compute_metrics,
    compute_pinball_loss,
)
from ecommerce_agent.business.forecast_models import (
    CrostonModel,
    EWMAForecastModel,
    RollingMeanModel,
    TSBModel,
    WeightedMovingAverageModel,
)
from ecommerce_agent.business.forecasting import (
    ForecastingService,
    LastValueModel,
    SevenDaySeasonalNaiveModel,
)


ROOT = Path(__file__).resolve().parent
MODELS = [
    LastValueModel(),
    SevenDaySeasonalNaiveModel(),
    RollingMeanModel(),
    WeightedMovingAverageModel(),
    EWMAForecastModel(),
    CrostonModel(),
    TSBModel(),
]


def _error_scale(history: list[Decimal]) -> Decimal:
    if len(history) < 8:
        return Decimal("0")
    errors = [abs(history[index] - history[index - 7]) for index in range(7, len(history))]
    return sum(errors, Decimal("0")) / Decimal(len(errors))


def run_suite(path: Path = ROOT / "fixtures.json") -> dict[str, Any]:
    fixture = json.loads(path.read_text("utf-8"))
    results: list[dict[str, Any]] = []
    for scenario in fixture["scenarios"]:
        history = [Decimal(str(value)) for value in scenario["history"]]
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(history))]
        if scenario["kind"] == "cold_start":
            results.append(
                {
                    "id": scenario["id"],
                    "status": "cold_start",
                    "demand_type": "cold_start",
                    "backtest_required": False,
                    "passed": True,
                }
            )
            continue
        report = RollingBacktest.run(history, dates, models=MODELS, forecast_horizon=7, windows=3)
        decision = ChampionSelector.select(report, baseline_name="last_value", improvement_threshold=Decimal("0.05"))
        champion = next(item for item in MODELS if item.name == decision.champion_model)
        forecast = champion.predict(history, 7)
        actual = [Decimal(str(value)) for value in scenario["future"]]
        metrics = compute_metrics(actual, forecast)
        error_scale = _error_scale(history)
        p80 = [max(value, value + error_scale) for value in forecast]
        p95 = [max(upper, value + error_scale * Decimal("2")) for value, upper in zip(forecast, p80)]
        intervals_monotonic = all(
            Decimal("0") <= value <= upper <= high
            for value, upper, high in zip(forecast, p80, p95)
        )
        coverage = {
            "p80": compute_interval_coverage(
                actual, lower=[Decimal("0")] * len(actual), upper=p80
            ),
            "p95": compute_interval_coverage(
                actual, lower=[Decimal("0")] * len(actual), upper=p95
            ),
        }
        pinball = {
            "p50": compute_pinball_loss(actual, forecast, Decimal("0.5")),
            "p80": compute_pinball_loss(actual, p80, Decimal("0.8")),
            "p95": compute_pinball_loss(actual, p95, Decimal("0.95")),
        }
        no_future_leakage = all(record.training_end < record.forecast_start for record in report.records)
        demand_type = ForecastingService._classify_demand(history)
        results.append({
            "id": scenario["id"],
            "champion": decision.champion_model,
            "reason": decision.reason,
            "metrics": {key: str(value) if value is not None else None for key, value in metrics.items()},
            "demand_type": demand_type["kind"],
            "intervals_monotonic": intervals_monotonic,
            "interval_coverage": {key: str(value) for key, value in coverage.items()},
            "pinball_loss": {key: str(value) for key, value in pinball.items()},
            "backtest_windows": len(report.records),
            "no_future_leakage": no_future_leakage,
            "passed": (
                intervals_monotonic
                and decision.champion_model is not None
                and bool(report.records)
                and no_future_leakage
                and all(Decimal("0") <= value <= Decimal("1") for value in coverage.values())
            ),
        })
    passed = sum(1 for item in results if item["passed"])
    fallback_result = next(item for item in results if item["id"] == "zero_demand")
    leakage_gate = all(item.get("no_future_leakage", True) for item in results)
    return {
        "suite_id": fixture["suite_id"],
        "virtual": fixture["virtual"],
        "production_claim": fixture["production_claim"],
        "ground_truth_isolated": fixture["ground_truth_isolated"],
        "scenarios": results,
        "passed": passed,
        "total": len(results),
        "gate": {
            "passed": passed == len(results) and leakage_gate,
            "no_future_leakage": leakage_gate,
            "baseline_fallback": "baseline_fallback" in fallback_result.get("reason", ""),
            "interval_coverage_bounded": all(
                all(Decimal("0") <= Decimal(value) <= Decimal("1") for value in item.get("interval_coverage", {}).values())
                for item in results
                if item.get("interval_coverage")
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures.json")
    args = parser.parse_args()
    print(json.dumps(run_suite(args.fixture), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
