from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ecommerce_agent.business.forecast_models import ForecastModel


@dataclass(frozen=True)
class DemandProfile:
    kind: str
    quality_flags: tuple[str, ...]
    zero_ratio: Decimal


@dataclass(frozen=True)
class ForecastMetrics:
    wape: Decimal | None
    bias: Decimal | None
    mae: Decimal


@dataclass(frozen=True)
class BacktestRecord:
    model: str
    training_end: date
    forecast_start: date
    metrics: ForecastMetrics
    failure: str | None = None


@dataclass(frozen=True)
class BacktestReport:
    records: tuple[BacktestRecord, ...]


@dataclass(frozen=True)
class ChampionDecision:
    champion: str
    baseline_protected: bool
    scores: dict[str, Decimal]


def compute_metrics(actual: list[Decimal], forecast: list[Decimal]) -> ForecastMetrics:
    if not actual or len(actual) != len(forecast):
        raise ValueError("forecast_metric_length_mismatch")
    absolute_error = sum((abs(a - f) for a, f in zip(actual, forecast)), Decimal("0"))
    signed_error = sum((f - a for a, f in zip(actual, forecast)), Decimal("0"))
    actual_total = sum((abs(value) for value in actual), Decimal("0"))
    count = Decimal(len(actual))
    return ForecastMetrics(
        wape=absolute_error / actual_total if actual_total else None,
        bias=signed_error / actual_total if actual_total else None,
        mae=absolute_error / count,
    )


def _coefficient_of_variation(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    if mean == 0:
        return Decimal("0")
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values))
    return variance.sqrt() / abs(mean)


def rolling_backtest(
    values: list[Decimal],
    dates: list[date],
    models: list[ForecastModel],
    *,
    horizon_days: int = 7,
    windows: int = 4,
) -> BacktestReport:
    if len(values) != len(dates):
        raise ValueError("forecast_series_length_mismatch")
    if horizon_days < 1 or windows < 1:
        raise ValueError("forecast_invalid_backtest_window")
    first_origin = max(1, len(values) - horizon_days * windows)
    origins = list(range(first_origin, len(values) - horizon_days + 1, horizon_days))[-windows:]
    records: list[BacktestRecord] = []
    for origin in origins:
        training, actual = values[:origin], values[origin : origin + horizon_days]
        for model in models:
            if len(training) < model.minimum_history_days:
                continue
            try:
                forecast = model.predict(training, len(actual))
                if len(forecast) != len(actual) or any(value < 0 for value in forecast):
                    raise ValueError("forecast_invalid_output")
                metrics = compute_metrics(actual, forecast)
                failure = None
            except (ArithmeticError, ValueError) as exc:
                metrics = ForecastMetrics(None, None, Decimal("Infinity"))
                failure = str(exc)
            records.append(
                BacktestRecord(
                    model=model.name,
                    training_end=dates[origin - 1],
                    forecast_start=dates[origin],
                    metrics=metrics,
                    failure=failure,
                )
            )
    return BacktestReport(tuple(records))


def select_champion(
    report: BacktestReport,
    *,
    baseline_name: str,
    improvement_threshold: Decimal = Decimal("0.05"),
) -> ChampionDecision:
    grouped: dict[str, list[Decimal]] = {}
    for record in report.records:
        if record.failure:
            continue
        score = record.metrics.wape if record.metrics.wape is not None else record.metrics.mae
        grouped.setdefault(record.model, []).append(score)
    scores = {
        name: sum(items, Decimal("0")) / Decimal(len(items))
        for name, items in grouped.items()
        if items
    }
    if baseline_name not in scores:
        raise ValueError("forecast_baseline_unavailable")
    best_name = min(scores, key=lambda name: (scores[name], name))
    baseline_score = scores[baseline_name]
    improved = (
        best_name != baseline_name
        and baseline_score > 0
        and scores[best_name] <= baseline_score * (Decimal("1") - improvement_threshold)
    )
    champion = best_name if improved else baseline_name
    return ChampionDecision(
        champion,
        baseline_protected=not improved or scores[champion] <= baseline_score,
        scores=scores,
    )


def classify_demand(
    values: list[Decimal],
    *,
    quality_flags: tuple[str, ...] = (),
) -> DemandProfile:
    if len(values) < 7:
        return DemandProfile("cold_start", tuple(sorted(quality_flags)), Decimal("0"))
    zero_ratio = Decimal(sum(value == 0 for value in values)) / Decimal(len(values))
    if all(value == 0 for value in values):
        return DemandProfile("zero", tuple(sorted(quality_flags)), zero_ratio)

    nonzero = [value for value in values if value > 0]
    nonzero_cv = _coefficient_of_variation(nonzero)
    if zero_ratio >= Decimal("0.40"):
        kind = "lumpy" if nonzero_cv >= Decimal("0.65") else "intermittent"
        return DemandProfile(kind, tuple(sorted(quality_flags)), zero_ratio)

    third = max(2, len(values) // 3)
    first = sum(values[:third], Decimal("0")) / Decimal(third)
    middle_values = values[third : third * 2]
    last_values = values[-third:]
    middle = sum(middle_values, Decimal("0")) / Decimal(len(middle_values))
    last = sum(last_values, Decimal("0")) / Decimal(len(last_values))
    scale = max(abs(first), abs(middle), abs(last), Decimal("1"))
    if (
        abs(last - middle) / scale >= Decimal("0.45")
        and abs(middle - first) / scale <= Decimal("0.15")
    ):
        return DemandProfile("regime_shift", tuple(sorted(quality_flags)), zero_ratio)

    mean = sum(values, Decimal("0")) / Decimal(len(values))
    coefficient = _coefficient_of_variation(values)
    if coefficient <= Decimal("0.12"):
        return DemandProfile("stable", tuple(sorted(quality_flags)), zero_ratio)

    daily_error = sum(
        (abs(values[index] - values[index - 1]) for index in range(1, len(values))),
        Decimal("0"),
    ) / Decimal(len(values) - 1)
    weekly_error = sum(
        (abs(values[index] - values[index - 7]) for index in range(7, len(values))),
        Decimal("0"),
    ) / Decimal(len(values) - 7)
    weekly = weekly_error <= daily_error * Decimal("0.75")
    if weekly:
        first_week = sum(values[:7], Decimal("0")) / Decimal(7)
        last_week = sum(values[-7:], Decimal("0")) / Decimal(7)
        weekly_change = abs(last_week - first_week) / max(abs(first_week), Decimal("1"))
        kind = "trend_weekly_seasonal" if weekly_change >= Decimal("0.20") else "weekly_seasonal"
        return DemandProfile(kind, tuple(sorted(quality_flags)), zero_ratio)

    quarter = max(2, len(values) // 4)
    start_mean = sum(values[:quarter], Decimal("0")) / Decimal(quarter)
    end_mean = sum(values[-quarter:], Decimal("0")) / Decimal(quarter)
    change = (end_mean - start_mean) / max(abs(start_mean), Decimal("1"))
    if change >= Decimal("0.25"):
        kind = "trend_up"
    elif change <= Decimal("-0.25"):
        kind = "trend_down"
    else:
        kind = "volatile" if coefficient >= Decimal("0.55") else "stable"
    return DemandProfile(kind, tuple(sorted(quality_flags)), zero_ratio)
