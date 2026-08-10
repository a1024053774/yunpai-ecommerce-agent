from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ecommerce_agent.business.forecast_evaluation import (
    classify_demand,
    compute_metrics,
    rolling_backtest,
    select_champion,
)
from ecommerce_agent.business.forecast_models import DEFAULT_FORECAST_MODELS, ForecastModel


ROOT = Path(__file__).resolve().parent


def _series(spec: dict[str, Any]) -> list[Decimal]:
    generator = spec["generator"]
    if generator == "repeat":
        values = spec["pattern"] * int(spec["cycles"])
    elif generator == "constant":
        values = [spec["value"]] * int(spec["length"])
    elif generator == "linear":
        values = [spec["start"] + spec["step"] * index for index in range(int(spec["length"]))]
    elif generator == "seasonal_trend":
        values = []
        offset = int(spec.get("cycle_offset", 0))
        for cycle in range(int(spec["cycles"])):
            shift = (cycle + offset) * spec["cycle_step"]
            values.extend(value + shift for value in spec["pattern"])
    elif generator == "segments":
        values = [
            segment["value"]
            for segment in spec["segments"]
            for _ in range(int(segment["length"]))
        ]
    else:
        raise ValueError(f"forecast_unknown_generator:{generator}")
    floor = Decimal(str(spec.get("floor", 0)))
    return [max(floor, Decimal(str(value))) for value in values]


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def run_suite(
    path: Path = ROOT / "scenarios.json",
    *,
    models: list[ForecastModel] | None = None,
    baseline_name: str | None = None,
) -> dict[str, Any]:
    fixture = json.loads(path.read_text("utf-8"))
    using_default_models = models is None
    candidates = list(DEFAULT_FORECAST_MODELS if models is None else models)
    results: list[dict[str, Any]] = []
    all_no_leakage = True
    all_baseline_protected = True

    for scenario in fixture["scenarios"]:
        history = _series(scenario["history"])
        future = _series(scenario["future"])
        flags = tuple(scenario.get("quality_flags", ()))
        profile = classify_demand(history, quality_flags=flags)
        failures: list[str] = []
        if profile.kind != scenario["expected_type"]:
            failures.append("demand_type_mismatch")

        expected_status = scenario.get("expected_status", "evaluated")
        if expected_status == "cold_start":
            if profile.kind != "cold_start":
                failures.append("cold_start_not_detected")
            results.append({
                "id": scenario["id"],
                "status": "failed" if failures else "cold_start",
                "demand_type": profile.kind,
                "quality_flags": list(profile.quality_flags),
                "champion": None,
                "metrics": None,
                "failures": failures,
                "passed": not failures,
            })
            continue

        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(history))]
        try:
            allowed = set(scenario.get("model_allowlist", ()))
            scenario_models = (
                [model for model in candidates if model.name in allowed]
                if using_default_models and allowed
                else candidates
            )
            by_name = {model.name: model for model in scenario_models}
            backtest = rolling_backtest(history, dates, scenario_models, horizon_days=7, windows=4)
            scenario_baseline = baseline_name or scenario.get("baseline", "last_value")
            decision = select_champion(backtest, baseline_name=scenario_baseline)
            champion = by_name[decision.champion]
            forecast = champion.predict(history, len(future))
            metrics = compute_metrics(future, forecast)
            no_leakage = bool(backtest.records) and all(
                record.training_end < record.forecast_start for record in backtest.records
            )
        except (KeyError, ValueError) as exc:
            failures.append(str(exc))
            decision = None
            metrics = None
            no_leakage = False
            forecast = []

        all_no_leakage = all_no_leakage and no_leakage
        if not no_leakage:
            failures.append("future_leakage_or_no_backtest")
        if decision is not None:
            all_baseline_protected = all_baseline_protected and decision.baseline_protected

        if expected_status == "degraded":
            if not flags:
                failures.append("degraded_without_quality_flag")
        elif metrics is not None:
            if scenario.get("expected_all_zero") and any(forecast):
                failures.append("zero_demand_nonzero_forecast")
            max_wape = scenario.get("max_wape")
            if max_wape is not None and (metrics.wape is None or metrics.wape > Decimal(max_wape)):
                failures.append("wape_exceeded")
            max_bias = scenario.get("max_abs_bias")
            if max_bias is not None and (
                metrics.bias is None or abs(metrics.bias) > Decimal(max_bias)
            ):
                failures.append("bias_exceeded")

        status = "failed" if failures else expected_status
        results.append({
            "id": scenario["id"],
            "status": status,
            "demand_type": profile.kind,
            "quality_flags": list(profile.quality_flags),
            "champion": decision.champion if decision else None,
            "metrics": ({
                "wape": _text(metrics.wape),
                "bias": _text(metrics.bias),
                "mae": _text(metrics.mae),
            } if metrics else None),
            "failures": failures,
            "passed": not failures,
        })

    passed = sum(item["passed"] for item in results)
    expectations_met = passed == len(results)
    return {
        "suite_id": fixture["suite_id"],
        "virtual": fixture["virtual"],
        "production_claim": fixture["production_claim"],
        "ground_truth_isolated": fixture["ground_truth_isolated"],
        "scenarios": results,
        "passed": passed,
        "total": len(results),
        "gate": {
            "passed": expectations_met and all_no_leakage and all_baseline_protected,
            "no_future_leakage": all_no_leakage,
            "all_scenario_expectations_met": expectations_met,
            "baseline_protected": all_baseline_protected,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic synthetic demand forecast evaluation"
    )
    parser.add_argument("--fixture", type=Path, default=ROOT / "scenarios.json")
    args = parser.parse_args()
    report = run_suite(args.fixture)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
