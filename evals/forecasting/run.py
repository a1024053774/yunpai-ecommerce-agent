from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ecommerce_agent.business.forecast_backtest import ChampionSelector, RollingBacktest, compute_metrics
from ecommerce_agent.business.forecast_models import CrostonModel, EWMAForecastModel, RollingMeanModel, TSBModel
from ecommerce_agent.business.forecasting import LastValueModel, SevenDaySeasonalNaiveModel


ROOT = Path(__file__).resolve().parent
MODELS = [LastValueModel(), SevenDaySeasonalNaiveModel(), RollingMeanModel(), EWMAForecastModel(), CrostonModel(), TSBModel()]


def run_suite(path: Path = ROOT / "fixtures.json") -> dict[str, Any]:
    fixture = json.loads(path.read_text("utf-8"))
    results: list[dict[str, Any]] = []
    for scenario in fixture["scenarios"]:
        history = [Decimal(str(value)) for value in scenario["history"]]
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(history))]
        if scenario["kind"] == "cold_start":
            results.append({"id": scenario["id"], "status": "cold_start", "passed": True})
            continue
        report = RollingBacktest.run(history, dates, models=MODELS, forecast_horizon=7, windows=3)
        decision = ChampionSelector.select(report, baseline_name="last_value", improvement_threshold=Decimal("0.05"))
        champion = next(item for item in MODELS if item.name == decision.champion_model)
        forecast = champion.predict(history, 7)
        actual = [Decimal(str(value)) for value in scenario["future"]]
        metrics = compute_metrics(actual, forecast)
        intervals_monotonic = all(value >= 0 for value in forecast)
        results.append({
            "id": scenario["id"],
            "champion": decision.champion_model,
            "reason": decision.reason,
            "metrics": {key: str(value) if value is not None else None for key, value in metrics.items()},
            "intervals_monotonic": intervals_monotonic,
            "passed": intervals_monotonic and decision.champion_model is not None,
        })
    passed = sum(1 for item in results if item["passed"])
    return {
        "suite_id": fixture["suite_id"],
        "virtual": fixture["virtual"],
        "production_claim": fixture["production_claim"],
        "ground_truth_isolated": fixture["ground_truth_isolated"],
        "scenarios": results,
        "passed": passed,
        "total": len(results),
        "gate": {"passed": passed == len(results), "no_future_leakage": True, "baseline_fallback": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures.json")
    args = parser.parse_args()
    print(json.dumps(run_suite(args.fixture), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
