from __future__ import annotations

from evals.forecasting.run import run_suite


def test_forecasting_synthetic_eval_is_isolated_and_has_all_scenarios() -> None:
    report = run_suite()
    assert report["virtual"] is True
    assert report["production_claim"] is False
    assert report["ground_truth_isolated"] is True
    assert report["total"] == 10
    assert report["passed"] == report["total"]
    assert report["gate"]["passed"] is True
    assert report["gate"]["no_future_leakage"] is True
    assert report["gate"]["baseline_fallback"] is True
    assert report["gate"]["interval_coverage_bounded"] is True
    assert {item["id"] for item in report["scenarios"]} >= {
        "promotion_peak",
        "stockout_truncated",
    }
