from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..database import Database
from .demand_facts import DemandFactService
from .forecast_backtest import ChampionSelector, RollingBacktest
from .forecast_models import CrostonModel, EWMAForecastModel, RollingMeanModel, TSBModel
from .inventory import InventoryService
from .source_versioning import payload_digest


ForecastHorizon = Literal[7, 14, 30]
ServiceLevel = Literal["p50", "p80", "p95"]


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    warehouse_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    horizon_days: ForecastHorizon = 7
    history_days: int = Field(default=56, ge=14, le=365)
    lead_time_days: int = Field(default=7, ge=1, le=180)
    review_period_days: int = Field(default=7, ge=1, le=180)
    service_level: ServiceLevel = "p80"
    safety_stock_days: int = Field(default=3, ge=0, le=180)
    minimum_order_qty: Decimal = Field(default=Decimal("0"), ge=0)
    order_multiple: Decimal = Field(default=Decimal("1"), gt=0)


class ForecastRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    horizon_days: ForecastHorizon = 7
    start_date: date | None = None
    end_date: date | None = None
    minimum_history_days: int = Field(default=14, ge=3, le=3650)
    candidate_models: list[str] = Field(
        default_factory=lambda: [
            "last_value",
            "7_day_seasonal_naive",
            "rolling_mean",
            "ewma",
            "croston",
            "tsb",
        ],
        min_length=1,
        max_length=12,
    )
    backtest_windows: int = Field(default=3, ge=1, le=30)
    improvement_threshold: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)

    @model_validator(mode="after")
    def validate_dates(self) -> "ForecastRunRequest":
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("forecast_invalid_date_range")
        return self


class ForecastOrderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["forecast_replenishment"] = "forecast_replenishment"
    status: Literal["draft"] = "draft"
    persisted: Literal[False] = False
    external_order_created: Literal[False] = False
    store_id: str
    warehouse_id: str
    sku_id: str
    recommended_quantity: str
    expected_stockout_date: str | None
    recommended_arrival_date: str
    service_level: ServiceLevel
    forecast_basis: dict[str, str | None]


@dataclass(frozen=True)
class DemandPoint:
    business_date: date
    gross_units: Decimal
    eligible_units: Decimal
    has_source_row: bool


@dataclass(frozen=True)
class DemandSeries:
    points: list[DemandPoint]
    policy_version: str
    timezone: str
    missing_business_dates: list[date]
    source_watermark: str | None
    total_eligible_units: Decimal
    total_gross_units: Decimal


class ForecastModel(Protocol):
    name: str
    minimum_history_days: int

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]: ...


class LastValueModel:
    name = "last_value"
    minimum_history_days = 1

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if not history:
            raise ValueError("forecast_insufficient_history")
        return [max(Decimal("0"), history[-1]) for _ in range(horizon_days)]


class SevenDaySeasonalNaiveModel:
    name = "7_day_seasonal_naive"
    minimum_history_days = 14

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if len(history) < self.minimum_history_days:
            raise ValueError("forecast_insufficient_history")
        pattern = history[-7:]
        return [max(Decimal("0"), pattern[index % 7]) for index in range(horizon_days)]


class ForecastModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ForecastModel] = {
            LastValueModel.name: LastValueModel(),
            SevenDaySeasonalNaiveModel.name: SevenDaySeasonalNaiveModel(),
            RollingMeanModel.name: RollingMeanModel(),
            EWMAForecastModel.name: EWMAForecastModel(),
            CrostonModel.name: CrostonModel(),
            TSBModel.name: TSBModel(),
        }

    def get(self, name: str) -> ForecastModel:
        try:
            return self._models[name]
        except KeyError as exc:
            raise ValueError("forecast_model_not_found") from exc

    def register(self, model: ForecastModel) -> None:
        self._models[model.name] = model


class ForecastingService:
    POLICY_VERSION = "demand-v1"
    TIMEZONE = "Asia/Shanghai"

    def __init__(
        self,
        db: Database,
        *,
        inventory_service: InventoryService | None = None,
        demand_fact_service: DemandFactService | None = None,
        model_registry: ForecastModelRegistry | None = None,
    ):
        self.db = db
        # Asia/Shanghai has a fixed UTC+8 offset; using a fixed offset keeps the
        # appliance runtime independent of an optional system tzdata package.
        self.zone = timezone(timedelta(hours=8))
        self.inventory_service = inventory_service or InventoryService(db)
        self.demand_facts = demand_fact_service
        self.models = model_registry or ForecastModelRegistry()

    def build_demand_series(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        history_days: int = 56,
    ) -> DemandSeries:
        rows = self._order_line_rows(tenant_id, store_id=store_id, sku_id=sku_id)
        if not rows:
            raise ValueError("forecast_insufficient_history")

        gross_by_day: dict[date, Decimal] = {}
        eligible_by_day: dict[date, Decimal] = {}
        source_rows_by_day: dict[date, bool] = {}
        watermark: str | None = None
        for row in rows:
            business_day = self._business_date(str(row["placed_at"]))
            quantity = Decimal(str(row["quantity"]))
            gross_by_day[business_day] = gross_by_day.get(business_day, Decimal("0")) + quantity
            if row["order_status"] != "canceled" and row["payment_status"] in {
                "paid",
                "partially_refunded",
            }:
                eligible_by_day[business_day] = (
                    eligible_by_day.get(business_day, Decimal("0")) + quantity
                )
            source_rows_by_day[business_day] = True
            source_time = str(row["source_updated_at"])
            if watermark is None or source_time > watermark:
                watermark = source_time

        first_day = min(gross_by_day)
        last_day = max(gross_by_day)
        window_start = max(first_day, last_day - timedelta(days=history_days - 1))
        points: list[DemandPoint] = []
        missing: list[date] = []
        cursor = window_start
        while cursor <= last_day:
            has_row = cursor in source_rows_by_day
            if not has_row:
                missing.append(cursor)
            points.append(
                DemandPoint(
                    business_date=cursor,
                    gross_units=gross_by_day.get(cursor, Decimal("0")),
                    eligible_units=eligible_by_day.get(cursor, Decimal("0")),
                    has_source_row=has_row,
                )
            )
            cursor += timedelta(days=1)

        return DemandSeries(
            points=points,
            policy_version=self.POLICY_VERSION,
            timezone=self.TIMEZONE,
            missing_business_dates=missing,
            source_watermark=watermark,
            total_eligible_units=sum((item.eligible_units for item in points), Decimal("0")),
            total_gross_units=sum((item.gross_units for item in points), Decimal("0")),
        )

    def preview(self, tenant_id: str, request: ForecastRequest) -> dict[str, Any]:
        series = self.build_demand_series(
            tenant_id,
            store_id=request.store_id,
            sku_id=request.sku_id,
            history_days=request.history_days,
        )
        history = [item.eligible_units for item in series.points]
        if len(history) < SevenDaySeasonalNaiveModel.minimum_history_days:
            raise ValueError("forecast_insufficient_history")

        balance = self._inventory_balance(
            tenant_id,
            store_id=request.store_id,
            warehouse_id=request.warehouse_id,
            sku_id=request.sku_id,
        )
        planning_days = request.lead_time_days + request.review_period_days
        forecast_days = max(request.horizon_days, planning_days)
        model = self.models.get(SevenDaySeasonalNaiveModel.name)
        point_forecast = model.predict(history, forecast_days)
        error_scale = self._seasonal_error_scale(history)
        forecast_points = self._forecast_points(series.points[-1].business_date, point_forecast, error_scale)

        selected = [Decimal(item[request.service_level]) for item in forecast_points[:planning_days]]
        target_demand = sum(selected, Decimal("0"))
        daily_demand = sum(point_forecast, Decimal("0")) / Decimal(len(point_forecast))
        safety_stock = daily_demand * Decimal(request.safety_stock_days)
        on_hand = Decimal(str(balance["on_hand"]))
        reserved = Decimal(str(balance["reserved"]))
        inbound = Decimal(str(balance["inbound"]))
        available = max(Decimal("0"), on_hand - reserved)
        raw_quantity = max(Decimal("0"), target_demand + safety_stock - available - inbound)
        rounded_quantity = self._round_order_quantity(
            raw_quantity,
            minimum=request.minimum_order_qty,
            multiple=request.order_multiple,
        )
        forecast_order = ForecastOrderDraft(
            store_id=request.store_id,
            warehouse_id=request.warehouse_id,
            sku_id=request.sku_id,
            recommended_quantity=self._decimal(rounded_quantity),
            expected_stockout_date=self._expected_stockout_date(
                forecast_points,
                service_level=request.service_level,
                supply=available + inbound,
            ),
            recommended_arrival_date=(
                series.points[-1].business_date + timedelta(days=request.lead_time_days)
            ).isoformat(),
            service_level=request.service_level,
            forecast_basis={
                "model": model.name,
                "data_watermark": series.source_watermark,
                "demand_policy_version": series.policy_version,
            },
        )
        return {
            "status": "draft",
            "persisted": False,
            "external_order_created": False,
            "store_id": request.store_id,
            "warehouse_id": request.warehouse_id,
            "sku_id": request.sku_id,
            "model": {
                "name": model.name,
                "version": "baseline-v1",
                "interval_method": "heuristic_error_band_v1",
                "calibrated": False,
            },
            "policy": {
                "demand_policy_version": series.policy_version,
                "timezone": series.timezone,
                "horizon_days": request.horizon_days,
                "planning_days": planning_days,
                "service_level": request.service_level,
            },
            "forecast_points": forecast_points,
            "data_quality": {
                "history_days": len(series.points),
                "missing_business_dates": [item.isoformat() for item in series.missing_business_dates],
                "source_watermark": series.source_watermark,
                "stockout_flag": "unknown",
            },
            "inventory": {
                "balance_id": balance["id"],
                "on_hand": self._decimal(on_hand),
                "reserved": self._decimal(reserved),
                "inbound": self._decimal(inbound),
                "available": self._decimal(available),
                "source_updated_at": balance["source_updated_at"],
            },
            "replenishment": {
                "target_demand": self._decimal(target_demand),
                "safety_stock": self._decimal(safety_stock),
                "raw_order_qty": self._decimal(raw_quantity),
                "recommended_order_qty": self._decimal(rounded_quantity),
                "risk": self._risk(available, daily_demand),
                "rounding": {
                    "minimum_order_qty": self._decimal(request.minimum_order_qty),
                    "order_multiple": self._decimal(request.order_multiple),
                },
            },
            "forecast_order": forecast_order.model_dump(mode="json"),
        }

    def run(self, tenant_id: str, request: ForecastRunRequest) -> dict[str, Any]:
        if self.demand_facts is None:
            raise ValueError("demand_fact_service_unavailable")
        facts = self.demand_facts.list_facts(
            tenant_id,
            store_id=request.store_id,
            sku_id=request.sku_id,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        if len(facts) < request.minimum_history_days:
            raise ValueError("forecast_insufficient_history")
        values = [Decimal(str(item["eligible_units"])) for item in facts]
        dates = [date.fromisoformat(str(item["business_date"])) for item in facts]
        models = [self.models.get(name) for name in request.candidate_models]
        report = RollingBacktest.run(
            values,
            dates,
            models=models,
            forecast_horizon=request.horizon_days,
            windows=request.backtest_windows,
        )
        baseline_name = "last_value" if "last_value" in request.candidate_models else models[0].name
        decision = ChampionSelector.select(
            report,
            baseline_name=baseline_name,
            improvement_threshold=request.improvement_threshold,
        )
        if decision.champion_model is None:
            raise ValueError("forecast_all_models_failed")
        champion = self.models.get(decision.champion_model)
        values_forecast = champion.predict(values, request.horizon_days)
        error_scale = self._seasonal_error_scale(values)
        points = self._forecast_points(dates[-1], values_forecast, error_scale)
        if any(
            Decimal(point["p50"]) > Decimal(point["p80"])
            or Decimal(point["p80"]) > Decimal(point["p95"])
            for point in points
        ):
            raise ValueError("forecast_invalid_intervals")
        run_id = f"forecast-run-{uuid.uuid4().hex}"
        data_hash = payload_digest(
            {
                "facts": facts,
                "demand_policy_version": self.POLICY_VERSION,
                "candidate_models": request.candidate_models,
            }
        )
        champion_summary = report.summaries.get(decision.champion_model, {})
        now = datetime.now(timezone.utc).isoformat()
        policy_id = self._persist_forecast_policy(tenant_id, request, now)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO forecast_runs(
                    run_id, tenant_id, store_id, sku_id, training_start, training_end,
                    data_hash, demand_policy_version, forecast_policy_version,
                    candidate_models_json, champion_model, champion_reason, model_version,
                    wape, bias, smape, rmse, forecast_horizon, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    request.store_id,
                    request.sku_id,
                    dates[0].isoformat(),
                    dates[-1].isoformat(),
                    data_hash,
                    self.POLICY_VERSION,
                    str(request.backtest_windows),
                    json.dumps(request.candidate_models, sort_keys=True),
                    decision.champion_model,
                    decision.reason,
                    "forecast-v1",
                    self._json_decimal(champion_summary.get("wape")),
                    self._json_decimal(champion_summary.get("bias")),
                    self._json_decimal(champion_summary.get("smape")),
                    self._json_decimal(champion_summary.get("rmse")),
                    request.horizon_days,
                    "succeeded",
                    now,
                ),
            )
            for record in report.records:
                conn.execute(
                    """
                    INSERT INTO forecast_backtests(
                        backtest_id, tenant_id, run_id, store_id, sku_id, model_name,
                        origin_date, forecast_horizon, actual_json, forecast_json,
                        error_json, failure_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"forecast-backtest-{uuid.uuid4().hex}",
                        tenant_id,
                        run_id,
                        request.store_id,
                        request.sku_id,
                        record.model,
                        record.training_end.isoformat(),
                        record.forecast_horizon,
                        json.dumps([str(value) for value in record.actual]),
                        json.dumps([str(value) for value in record.forecast]),
                        json.dumps(
                            {
                                key: self._json_decimal(value)
                                for key, value in (record.metrics or {}).items()
                            },
                            sort_keys=True,
                        ),
                        record.failure_reason,
                        now,
                    ),
                )
            for point in points:
                conn.execute(
                    """
                    INSERT INTO forecast_points(
                        point_id, tenant_id, run_id, forecast_date, p50, p80, p95, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"forecast-point-{uuid.uuid4().hex}",
                        tenant_id,
                        run_id,
                        point["forecast_date"],
                        point["p50"],
                        point["p80"],
                        point["p95"],
                        now,
                    ),
                )
            quality = self._forecast_quality(facts)
            if quality["level"] != "good":
                conn.execute(
                    """
                    INSERT INTO forecast_anomalies(
                        anomaly_id, tenant_id, run_id, store_id, sku_id,
                        anomaly_type, evidence_json, disposition, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        f"forecast-anomaly-{uuid.uuid4().hex}",
                        tenant_id,
                        run_id,
                        request.store_id,
                        request.sku_id,
                        "demand_quality_degraded",
                        json.dumps(quality, sort_keys=True),
                        now,
                    ),
                )
        return self.get_run(tenant_id, run_id)

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM forecast_runs WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("forecast_run_not_found")
            points = conn.execute(
                "SELECT * FROM forecast_points WHERE tenant_id=? AND run_id=? ORDER BY forecast_date",
                (tenant_id, run_id),
            ).fetchall()
            backtests = conn.execute(
                "SELECT * FROM forecast_backtests WHERE tenant_id=? AND run_id=? ORDER BY model_name, origin_date",
                (tenant_id, run_id),
            ).fetchall()
            anomalies = conn.execute(
                "SELECT * FROM forecast_anomalies WHERE tenant_id=? AND run_id=? ORDER BY created_at",
                (tenant_id, run_id),
            ).fetchall()
        return {
            "run_id": row["run_id"],
            "tenant_id": row["tenant_id"],
            "store_id": row["store_id"],
            "sku_id": row["sku_id"],
            "training_start": row["training_start"],
            "training_end": row["training_end"],
            "data_hash": row["data_hash"],
            "demand_policy_version": row["demand_policy_version"],
            "forecast_policy_version": row["forecast_policy_version"],
            "candidate_models": json.loads(row["candidate_models_json"]),
            "champion_model": row["champion_model"],
            "champion_reason": row["champion_reason"],
            "model_version": row["model_version"],
            "metrics": {
                key: row[key] for key in ("wape", "bias", "smape", "rmse")
            },
            "forecast_horizon": row["forecast_horizon"],
            "status": row["status"],
            "forecast_points": [dict(item) for item in points],
            "backtests": [
                {
                    **dict(item),
                    "actual": json.loads(item["actual_json"]),
                    "forecast": json.loads(item["forecast_json"]),
                    "errors": json.loads(item["error_json"]),
                }
                for item in backtests
            ],
            "anomalies": [
                {**dict(item), "evidence": json.loads(item["evidence_json"])}
                for item in anomalies
            ],
            "data_quality": {
                "demand_policy_version": row["demand_policy_version"],
                "quality_level": "degraded" if anomalies else "good",
            },
        }

    def _persist_forecast_policy(
        self, tenant_id: str, request: ForecastRunRequest, active_from: str
    ) -> str:
        policy_id = f"forecast-policy-{tenant_id}-{request.store_id}-{request.sku_id}"
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                "SELECT policy_id, policy_version FROM forecast_policies WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
            version = int(existing["policy_version"]) + 1 if existing else 1
            conn.execute(
                """
                INSERT INTO forecast_policies(
                    policy_id, tenant_id, store_id, sku_id, horizons_json,
                    minimum_history_days, candidate_models_json, backtest_windows,
                    interval_levels_json, demand_policy_version, policy_version,
                    active_from, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    horizons_json=excluded.horizons_json,
                    minimum_history_days=excluded.minimum_history_days,
                    candidate_models_json=excluded.candidate_models_json,
                    backtest_windows=excluded.backtest_windows,
                    interval_levels_json=excluded.interval_levels_json,
                    policy_version=excluded.policy_version,
                    active_from=excluded.active_from
                """,
                (
                    policy_id,
                    tenant_id,
                    request.store_id,
                    request.sku_id,
                    json.dumps([7, 14, 30]),
                    request.minimum_history_days,
                    json.dumps(request.candidate_models),
                    request.backtest_windows,
                    json.dumps(["p50", "p80", "p95"]),
                    self.POLICY_VERSION,
                    version,
                    active_from,
                    active_from,
                ),
            )
        return policy_id

    @staticmethod
    def _forecast_quality(facts: list[dict[str, Any]]) -> dict[str, Any]:
        missing = sum(bool(item["quality"].get("missing_source_date")) for item in facts)
        unknown = sum(item["stockout_flag"] == "unknown" for item in facts)
        return {
            "level": "degraded" if missing or unknown else "good",
            "missing_source_dates": missing,
            "unknown_stockout_dates": unknown,
            "demand_policy_version": "demand-v1",
        }

    @staticmethod
    def _json_decimal(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _order_line_rows(self, tenant_id: str, *, store_id: str, sku_id: str) -> list[Any]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT o.placed_at, o.order_status, o.payment_status,
                       o.source_updated_at, l.quantity
                FROM commerce_orders o
                JOIN commerce_order_lines l ON l.order_id=o.id
                WHERE o.tenant_id=? AND o.store_id=? AND l.sku_id=?
                ORDER BY o.placed_at, o.id, l.external_line_id
                """,
                (tenant_id, store_id, sku_id),
            ).fetchall()

    def _inventory_balance(
        self,
        tenant_id: str,
        *,
        store_id: str,
        warehouse_id: str,
        sku_id: str,
    ) -> dict[str, Any]:
        balances = self.inventory_service.list_balances(
            tenant_id,
            store_id=store_id,
            sku_id=sku_id,
        )
        matches = [item for item in balances if item["warehouse_id"] == warehouse_id]
        if not matches:
            raise ValueError("inventory_balance_not_found")
        return matches[0]

    def _forecast_points(
        self, last_date: date, values: list[Decimal], error_scale: Decimal
    ) -> list[dict[str, str]]:
        points: list[dict[str, str]] = []
        for index, value in enumerate(values, start=1):
            p50 = max(Decimal("0"), value)
            p80 = max(p50, p50 + error_scale)
            p95 = max(p80, p50 + (error_scale * Decimal("2")))
            points.append(
                {
                    "forecast_date": (last_date + timedelta(days=index)).isoformat(),
                    "p50": self._decimal(p50),
                    "p80": self._decimal(p80),
                    "p95": self._decimal(p95),
                }
            )
        return points

    @staticmethod
    def _seasonal_error_scale(history: list[Decimal]) -> Decimal:
        if len(history) < 8:
            return Decimal("0")
        errors = [abs(history[index] - history[index - 7]) for index in range(7, len(history))]
        return sum(errors, Decimal("0")) / Decimal(len(errors))

    @staticmethod
    def _round_order_quantity(value: Decimal, *, minimum: Decimal, multiple: Decimal) -> Decimal:
        if value <= 0:
            return Decimal("0")
        rounded = max(value, minimum)
        units = (rounded / multiple).to_integral_value(rounding=ROUND_CEILING)
        return units * multiple

    @staticmethod
    def _risk(available: Decimal, daily_demand: Decimal) -> str:
        if available <= 0:
            return "stockout"
        if daily_demand <= 0:
            return "no_demand"
        coverage = available / daily_demand
        if coverage < 7:
            return "stockout_risk"
        if coverage < 14:
            return "replenishment_due"
        return "healthy"

    @staticmethod
    def _expected_stockout_date(
        forecast_points: list[dict[str, str]],
        *,
        service_level: ServiceLevel,
        supply: Decimal,
    ) -> str | None:
        remaining = supply
        for point in forecast_points:
            remaining -= Decimal(point[service_level])
            if remaining < 0:
                return point["forecast_date"]
        return None

    def _business_date(self, value: str) -> date:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(self.zone).date()

    @staticmethod
    def _decimal(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), "f")
