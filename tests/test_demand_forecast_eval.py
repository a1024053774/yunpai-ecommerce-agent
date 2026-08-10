from __future__ import annotations

from decimal import Decimal

from evals.forecasting.run import run_suite


REQUIRED_SCENARIOS = {
    "stable_noise",
    "trend_up",
    "trend_down",
    "weekly_seasonal",
    "trend_weekly_seasonal",
    "intermittent",
    "lumpy",
    "volatile",
    "zero_demand",
    "cold_start",
    "regime_shift",
    "promotion_without_calendar",
    "missing_dates",
    "stockout_truncated",
}


def test_synthetic_suite_covers_required_shapes_and_enforces_quality_gate() -> None:
    report = run_suite()

    assert report["virtual"] is True
    assert report["production_claim"] is False
    assert report["ground_truth_isolated"] is True
    assert {item["id"] for item in report["scenarios"]} >= REQUIRED_SCENARIOS
    assert report["passed"] == report["total"]
    assert report["gate"] == {
        "passed": True,
        "no_future_leakage": True,
        "all_scenario_expectations_met": True,
        "baseline_protected": True,
    }


def test_risk_scenarios_are_explicitly_degraded_instead_of_claiming_accuracy() -> None:
    report = run_suite()
    scenarios = {item["id"]: item for item in report["scenarios"]}

    assert scenarios["promotion_without_calendar"]["status"] == "degraded"
    assert "promotion_calendar_missing" in scenarios["promotion_without_calendar"]["quality_flags"]
    assert scenarios["stockout_truncated"]["status"] == "degraded"
    assert "stockout_censored" in scenarios["stockout_truncated"]["quality_flags"]
    assert scenarios["missing_dates"]["demand_type"] == "weekly_seasonal"
    assert scenarios["missing_dates"]["status"] == "degraded"


def test_bad_forecasts_fail_the_gate_even_when_the_pipeline_runs() -> None:
    class AlwaysZeroModel:
        name = "always_zero"
        minimum_history_days = 1

        def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
            return [Decimal("0")] * horizon_days

    report = run_suite(models=[AlwaysZeroModel()], baseline_name="always_zero")

    assert report["gate"]["no_future_leakage"] is True
    assert report["gate"]["passed"] is False
    assert any(
        item["status"] == "failed" and "wape_exceeded" in item["failures"]
        for item in report["scenarios"]
    )

