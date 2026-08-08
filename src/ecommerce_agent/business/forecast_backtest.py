from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .forecast_models import ForecastModel


def compute_metrics(actual: list[Decimal], forecast: list[Decimal]) -> dict[str, Decimal | None]:
    if len(actual) != len(forecast) or not actual:
        raise ValueError("forecast_metric_length_mismatch")
    absolute_error = sum((abs(a - f) for a, f in zip(actual, forecast)), Decimal("0"))
    actual_total = sum((abs(a) for a in actual), Decimal("0"))
    wape = absolute_error / actual_total if actual_total else None
    bias = sum((f - a for a, f in zip(actual, forecast)), Decimal("0"))
    bias = bias / actual_total if actual_total else None
    smape_terms = []
    for a, f in zip(actual, forecast):
        denominator = abs(a) + abs(f)
        smape_terms.append(Decimal("0") if denominator == 0 else Decimal("2") * abs(a - f) / denominator)
    smape = sum(smape_terms, Decimal("0")) / Decimal(len(smape_terms))
    rmse = (
        sum(((a - f) ** 2 for a, f in zip(actual, forecast)), Decimal("0"))
        / Decimal(len(actual))
    ).sqrt()
    return {"wape": wape, "bias": bias, "smape": smape, "rmse": rmse}


def compute_pinball_loss(
    actual: list[Decimal], forecast: list[Decimal], quantile: Decimal
) -> Decimal:
    if len(actual) != len(forecast) or not actual:
        raise ValueError("forecast_metric_length_mismatch")
    if not Decimal("0") < quantile < Decimal("1"):
        raise ValueError("forecast_quantile_invalid")
    loss = sum(
        (
            quantile * (value - prediction)
            if value >= prediction
            else (Decimal("1") - quantile) * (prediction - value)
        for value, prediction in zip(actual, forecast)
        ),
        Decimal("0"),
    )
    return loss / Decimal(len(actual))


def compute_interval_coverage(
    actual: list[Decimal], *, lower: list[Decimal], upper: list[Decimal]
) -> Decimal:
    if len(actual) != len(lower) or len(actual) != len(upper) or not actual:
        raise ValueError("forecast_metric_length_mismatch")
    if any(low > high for low, high in zip(lower, upper)):
        raise ValueError("forecast_interval_invalid")
    covered = sum(low <= value <= high for value, low, high in zip(actual, lower, upper))
    return Decimal(covered) / Decimal(len(actual))


@dataclass(frozen=True)
class BacktestRecord:
    model: str
    training_end: date
    forecast_start: date
    forecast_horizon: int
    actual: tuple[Decimal, ...]
    forecast: tuple[Decimal, ...]
    metrics: dict[str, Decimal | None] | None
    failure_reason: str | None


@dataclass(frozen=True)
class BacktestReport:
    records: tuple[BacktestRecord, ...]

    @property
    def summaries(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[BacktestRecord]] = {}
        for record in self.records:
            grouped.setdefault(record.model, []).append(record)
        result: dict[str, dict[str, Any]] = {}
        for model, records in grouped.items():
            successful = [record for record in records if record.metrics is not None]
            if not successful:
                result[model] = {"model": model, "windows": 0, "failed": len(records), "wape": None}
                continue
            values: dict[str, Decimal | None] = {}
            for metric in ("wape", "bias", "smape", "rmse"):
                numbers = [record.metrics[metric] for record in successful if record.metrics[metric] is not None]
                values[metric] = sum(numbers, Decimal("0")) / Decimal(len(numbers)) if numbers else None
            result[model] = {
                "model": model,
                "windows": len(successful),
                "failed": len(records) - len(successful),
                **values,
            }
        return result


class RollingBacktest:
    @staticmethod
    def run(
        values: list[Decimal],
        dates: list[date],
        *,
        models: Iterable[ForecastModel],
        forecast_horizon: int,
        windows: int = 3,
    ) -> BacktestReport:
        if len(values) != len(dates) or not values:
            raise ValueError("forecast_series_length_mismatch")
        if forecast_horizon < 1 or windows < 1:
            raise ValueError("forecast_backtest_parameters_invalid")
        candidates = list(models)
        minimum_history = max(model.minimum_history_days for model in candidates)
        origins = list(range(minimum_history, len(values) - forecast_horizon + 1))[-windows:]
        records: list[BacktestRecord] = []
        for model in candidates:
            for origin in origins:
                history = values[:origin]
                actual = values[origin : origin + forecast_horizon]
                try:
                    forecast = model.predict(history, forecast_horizon)
                    if len(forecast) != forecast_horizon:
                        raise ValueError("forecast_model_length_mismatch")
                    if any(value < 0 for value in forecast):
                        raise ValueError("forecast_negative_output")
                    metrics = compute_metrics(actual, forecast)
                    failure_reason = None
                except Exception as exc:  # candidate failure is non-blocking
                    forecast = []
                    metrics = None
                    failure_reason = str(exc)
                records.append(
                    BacktestRecord(
                        model=model.name,
                        training_end=dates[origin - 1],
                        forecast_start=dates[origin],
                        forecast_horizon=forecast_horizon,
                        actual=tuple(actual),
                        forecast=tuple(forecast),
                        metrics=metrics,
                        failure_reason=failure_reason,
                    )
                )
        return BacktestReport(records=tuple(records))


@dataclass(frozen=True)
class ChampionDecision:
    champion_model: str | None
    reason: str
    ranking: list[dict[str, Any]]


class ChampionSelector:
    @staticmethod
    def select(
        report: BacktestReport,
        *,
        baseline_name: str,
        improvement_threshold: Decimal = Decimal("0.05"),
    ) -> ChampionDecision:
        summaries = report.summaries
        ranking = sorted(
            summaries.values(),
            key=lambda item: (
                item["wape"] is None,
                item["wape"] if item["wape"] is not None else Decimal("Infinity"),
                0 if item["model"] == baseline_name else 1,
                item["model"],
            ),
        )
        successful = [item for item in ranking if item["wape"] is not None]
        if not successful:
            # A zero-demand window has no denominator for WAPE. Keep the
            # deterministic baseline rather than turning a usable zero forecast
            # into an unavailable model decision.
            if baseline_name in summaries:
                return ChampionDecision(baseline_name, "baseline_fallback_all_candidates_failed", ranking)
            if not summaries:
                return ChampionDecision(baseline_name, "baseline_fallback_no_backtest_window", ranking)
            return ChampionDecision(None, "all_candidates_failed", ranking)
        baseline = summaries.get(baseline_name)
        best = successful[0]
        if baseline is None or baseline["wape"] is None:
            return ChampionDecision(best["model"], "baseline_unavailable", ranking)
        if best["model"] == baseline_name:
            return ChampionDecision(baseline_name, "baseline_best", ranking)
        threshold = baseline["wape"] * (Decimal("1") - improvement_threshold)
        if best["wape"] > threshold:
            return ChampionDecision(baseline_name, "candidate_below_improvement_threshold", ranking)
        return ChampionDecision(best["model"], "candidate_improved_over_baseline", ranking)
