"""M10-R WP1-03 外生信号无泄漏滚动准入契约（确定性）。

只做准入 Gate，不重写预测算法：
- 每个 rolling origin 只用 ``≤ training_end`` 的信号；超过 data_as_of 的信号
  即判定未来泄漏并拒绝。
- 同窗比较 signal challenger 与 baseline，只有稳定优于 baseline 才准入。
- source_kind=actual 且准入才允许 operational champion；manual/demo 只形成
  evaluation 证据，operational 走 baseline 并标 signal_usage=not_used。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ..readonly_data.contracts import SourceKind


class SignalAdmission(StrEnum):
    ADMITTED = "admitted"
    REJECTED_NOT_BETTER = "rejected_not_better"
    REJECTED_FUTURE_LEAKAGE = "rejected_future_leakage"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class SignalGateResult:
    admission: SignalAdmission
    reason: str
    operational_champion: bool
    signal_usage: str
    comparisons: tuple[dict[str, Any], ...]
    data_as_of: str | None
    final_signal_factor: float | None = None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "admission": self.admission.value,
            "reason": self.reason,
            "operational_champion": self.operational_champion,
            "signal_usage": self.signal_usage,
            "data_as_of": self.data_as_of,
            "final_signal_factor": self.final_signal_factor,
            "comparisons": list(self.comparisons),
        }


def _wape(actual: Sequence[float], forecast: Sequence[float]) -> float | None:
    denominator = sum(actual)
    if denominator == 0:
        return None
    errors = [predicted - observed for observed, predicted in zip(actual, forecast, strict=True)]
    return round(sum(abs(value) for value in errors) / denominator, 9)


def _signal_factor(values: Sequence[float]) -> float:
    latest = values[-1]
    return min(1.5, max(0.5, 1.0 + (latest - 1.0)))


class SignalGate:
    """无泄漏滚动外生信号准入 Gate。"""

    def __init__(
        self,
        *,
        required_relative_improvement: float = 0.02,
        minimum_origins: int = 2,
    ) -> None:
        if not 0 < required_relative_improvement < 1:
            raise ValueError("signal_required_relative_improvement_invalid")
        if minimum_origins < 1:
            raise ValueError("signal_minimum_origins_invalid")
        self.required_relative_improvement = required_relative_improvement
        self.minimum_origins = minimum_origins

    def evaluate(
        self,
        *,
        baseline_rows: Sequence[dict[str, Any]],
        signal_by_date: Mapping[date, float],
        signal_as_of: Mapping[date, date] | None = None,
        source_kind: SourceKind,
        data_as_of: date | None,
    ) -> SignalGateResult:
        source_kind = SourceKind(source_kind)
        visible_cutoff = data_as_of
        future_dates = sorted(
            d
            for d in signal_by_date
            if visible_cutoff is not None and d > visible_cutoff
        )
        if future_dates:
            return SignalGateResult(
                admission=SignalAdmission.REJECTED_FUTURE_LEAKAGE,
                reason="signal_future_leakage",
                operational_champion=False,
                signal_usage="not_used",
                comparisons=(),
                data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
            )

        rows = [row for row in baseline_rows if row.get("failure_reason") is None]
        if len(rows) < self.minimum_origins:
            return SignalGateResult(
                admission=SignalAdmission.INSUFFICIENT_EVIDENCE,
                reason="signal_insufficient_origins",
                operational_champion=False,
                signal_usage="not_used",
                comparisons=(),
                data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
            )

        comparisons: list[dict[str, Any]] = []
        improved = True
        for row in rows:
            training_end = date.fromisoformat(str(row["training_end"]))
            visible_values = [
                value
                for signal_date, value in sorted(signal_by_date.items())
                if signal_date <= training_end
                and value is not None
                and (
                    signal_as_of is None
                    or signal_as_of.get(signal_date) is None
                    or signal_as_of.get(signal_date) <= training_end
                )
            ]
            if not visible_values:
                return SignalGateResult(
                    admission=SignalAdmission.INSUFFICIENT_EVIDENCE,
                    reason="signal_missing_for_origin",
                    operational_champion=False,
                    signal_usage="not_used",
                    comparisons=(),
                    data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
                )
            baseline_forecast = [float(value) for value in row["forecast"]]
            actual = [float(value) for value in row["actual"]]
            factor = _signal_factor(visible_values)
            challenger_forecast = [value * factor for value in baseline_forecast]
            baseline_wape = _wape(actual, baseline_forecast)
            challenger_wape = _wape(actual, challenger_forecast)
            if baseline_wape is None or challenger_wape is None:
                continue
            beat = (
                baseline_wape > 0
                and challenger_wape
                <= baseline_wape * (1 - self.required_relative_improvement)
            )
            improved = improved and beat
            comparisons.append(
                {
                    "origin": row.get("origin_date"),
                    "training_end": row["training_end"],
                    "signal_factor": round(factor, 9),
                    "baseline_wape": baseline_wape,
                    "challenger_wape": challenger_wape,
                    "improved": beat,
                }
            )

        if not comparisons:
            return SignalGateResult(
                admission=SignalAdmission.INSUFFICIENT_EVIDENCE,
                reason="signal_no_comparable_origins",
                operational_champion=False,
                signal_usage="not_used",
                comparisons=(),
                data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
            )

        if not improved:
            return SignalGateResult(
                admission=SignalAdmission.REJECTED_NOT_BETTER,
                reason="signal_not_better_than_baseline",
                operational_champion=False,
                signal_usage="not_used",
                comparisons=tuple(comparisons),
                data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
            )

        operational_champion = source_kind is SourceKind.ACTUAL
        signal_usage = "signal_used" if operational_champion else "evaluation_only"
        final_visible = [
            value
            for signal_date, value in sorted(signal_by_date.items())
            if value is not None
            and (visible_cutoff is None or signal_date <= visible_cutoff)
        ]
        final_signal_factor = _signal_factor(final_visible) if final_visible else None
        return SignalGateResult(
            admission=SignalAdmission.ADMITTED,
            reason="signal_improves_baseline_no_leakage",
            operational_champion=operational_champion,
            signal_usage=signal_usage,
            comparisons=tuple(comparisons),
            data_as_of=visible_cutoff.isoformat() if visible_cutoff else None,
            final_signal_factor=final_signal_factor,
        )
