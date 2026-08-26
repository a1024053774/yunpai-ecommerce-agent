from __future__ import annotations

from datetime import date

from ecommerce_agent.forecasting.signal_gate import (
    SignalAdmission,
    SignalGate,
)
from ecommerce_agent.readonly_data.contracts import SourceKind


def _row(origin: str, training_end: str, actual, forecast) -> dict:
    return {
        "origin_date": origin,
        "training_end": training_end,
        "actual": actual,
        "forecast": forecast,
        "failure_reason": None,
    }


def test_future_signal_is_rejected_as_leakage() -> None:
    gate = SignalGate()
    result = gate.evaluate(
        baseline_rows=[_row("2026-08-10", "2026-08-09", [10.0], [10.0])],
        signal_by_date={date(2026, 8, 10): 1.1},
        source_kind=SourceKind.ACTUAL,
        data_as_of=date(2026, 8, 9),
    )
    assert result.admission is SignalAdmission.REJECTED_FUTURE_LEAKAGE
    assert result.operational_champion is False
    assert result.signal_usage == "not_used"


def test_signal_worse_than_baseline_is_rejected() -> None:
    gate = SignalGate()
    result = gate.evaluate(
        baseline_rows=[
            _row("2026-08-10", "2026-08-09", [10.0, 10.0], [10.0, 10.0]),
            _row("2026-08-17", "2026-08-16", [10.0, 10.0], [10.0, 10.0]),
        ],
        signal_by_date={date(2026, 8, 9): 1.2, date(2026, 8, 16): 1.2},
        source_kind=SourceKind.ACTUAL,
        data_as_of=date(2026, 8, 16),
    )
    assert result.admission is SignalAdmission.REJECTED_NOT_BETTER


def test_late_signal_not_visible_at_origin_is_excluded() -> None:
    gate = SignalGate(minimum_origins=2)
    result = gate.evaluate(
        baseline_rows=[
            _row("2026-01-02", "2026-01-01", [10.0], [10.0]),
            _row("2026-01-09", "2026-01-08", [10.0], [10.0]),
        ],
        signal_by_date={date(2026, 1, 1): 1.5, date(2026, 1, 8): 1.5},
        signal_as_of={
            date(2026, 1, 1): date(2026, 1, 10),
            date(2026, 1, 8): date(2026, 1, 10),
        },
        source_kind=SourceKind.ACTUAL,
        data_as_of=date(2026, 1, 10),
    )
    assert result.admission is SignalAdmission.INSUFFICIENT_EVIDENCE
    assert result.reason == "signal_missing_for_origin"
    assert result.operational_champion is False


def test_improving_actual_signal_is_admitted_for_operational_champion() -> None:
    gate = SignalGate()
    result = gate.evaluate(
        baseline_rows=[
            _row("2026-08-10", "2026-08-09", [10.0, 10.0], [12.0, 12.0]),
            _row("2026-08-17", "2026-08-16", [10.0, 10.0], [12.0, 12.0]),
        ],
        signal_by_date={date(2026, 8, 9): 0.8, date(2026, 8, 16): 0.8},
        source_kind=SourceKind.ACTUAL,
        data_as_of=date(2026, 8, 16),
    )
    assert result.admission is SignalAdmission.ADMITTED
    assert result.operational_champion is True
    assert result.signal_usage == "signal_used"


def test_demo_signal_cannot_become_operational_champion() -> None:
    gate = SignalGate()
    result = gate.evaluate(
        baseline_rows=[
            _row("2026-08-10", "2026-08-09", [10.0, 10.0], [12.0, 12.0]),
            _row("2026-08-17", "2026-08-16", [10.0, 10.0], [12.0, 12.0]),
        ],
        signal_by_date={date(2026, 8, 9): 0.8, date(2026, 8, 16): 0.8},
        source_kind=SourceKind.DEMO,
        data_as_of=date(2026, 8, 16),
    )
    assert result.admission is SignalAdmission.ADMITTED
    assert result.operational_champion is False
    assert result.signal_usage == "evaluation_only"


def test_insufficient_origins_are_not_admitted() -> None:
    gate = SignalGate(minimum_origins=2)
    result = gate.evaluate(
        baseline_rows=[_row("2026-08-10", "2026-08-09", [10.0], [10.0])],
        signal_by_date={date(2026, 8, 9): 1.0},
        source_kind=SourceKind.ACTUAL,
        data_as_of=date(2026, 8, 9),
    )
    assert result.admission is SignalAdmission.INSUFFICIENT_EVIDENCE
