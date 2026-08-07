from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..database import Database, utc_now
from .forecasting import ForecastingService
from .inventory import InventoryService
from .source_versioning import payload_digest


ServiceLevel = Literal["p50", "p80", "p95"]


class InventoryPlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    warehouse_id: str | None = Field(default=None, max_length=128)
    supplier_lead_days: int = Field(default=7, ge=0, le=365)
    review_period_days: int = Field(default=7, ge=0, le=365)
    service_level: ServiceLevel = "p80"
    minimum_order_qty: Decimal = Field(default=Decimal("0"), ge=0)
    order_multiple: Decimal = Field(default=Decimal("1"), gt=0)
    minimum_safety_stock: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_stock_days: int | None = Field(default=None, ge=1, le=3650)
    policy_version: int = Field(default=1, ge=1)
    active_from: datetime | None = None

    @model_validator(mode="after")
    def validate_time(self) -> "InventoryPlanningPolicy":
        if self.active_from is not None and self.active_from.tzinfo is None:
            raise ValueError("inventory_policy_timezone_required")
        return self


class InventoryPlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=128)
    forecast_run_id: str = Field(min_length=1, max_length=128)


class InventoryPlanningService:
    def __init__(
        self,
        db: Database,
        *,
        inventory: InventoryService,
        forecasting: ForecastingService,
    ):
        self.db = db
        self.inventory = inventory
        self.forecasting = forecasting
        self.zone = timezone(timedelta(hours=8))

    def upsert_policy(
        self, tenant_id: str, policy: InventoryPlanningPolicy
    ) -> dict[str, Any]:
        policy_id = self._policy_id(tenant_id, policy)
        active_from = (
            policy.active_from.astimezone(timezone.utc).isoformat()
            if policy.active_from is not None
            else utc_now()
        )
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                "SELECT policy_version FROM inventory_planning_policies WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
            version = int(existing["policy_version"]) + 1 if existing else policy.policy_version
            conn.execute(
                """
                INSERT INTO inventory_planning_policies(
                    policy_id, tenant_id, store_id, sku_id, warehouse_id,
                    supplier_lead_days, review_period_days, service_level,
                    minimum_order_qty, order_multiple, minimum_safety_stock,
                    maximum_stock_days, policy_version, active_from, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    supplier_lead_days=excluded.supplier_lead_days,
                    review_period_days=excluded.review_period_days,
                    service_level=excluded.service_level,
                    minimum_order_qty=excluded.minimum_order_qty,
                    order_multiple=excluded.order_multiple,
                    minimum_safety_stock=excluded.minimum_safety_stock,
                    maximum_stock_days=excluded.maximum_stock_days,
                    policy_version=excluded.policy_version,
                    active_from=excluded.active_from
                """,
                (
                    policy_id,
                    tenant_id,
                    policy.store_id,
                    policy.sku_id,
                    policy.warehouse_id,
                    policy.supplier_lead_days,
                    policy.review_period_days,
                    policy.service_level,
                    str(policy.minimum_order_qty),
                    str(policy.order_multiple),
                    str(policy.minimum_safety_stock),
                    policy.maximum_stock_days,
                    version,
                    active_from,
                    utc_now(),
                ),
            )
        return self._policy_view(tenant_id, policy_id)

    def create_plan(
        self,
        tenant_id: str,
        *,
        forecast_run_id: str,
        warehouse_id: str,
    ) -> dict[str, Any]:
        run = self.forecasting.get_run(tenant_id, forecast_run_id)
        policy = self._select_policy(
            tenant_id,
            store_id=run["store_id"],
            sku_id=run["sku_id"],
            warehouse_id=warehouse_id,
        )
        balances = self.inventory.list_balances(
            tenant_id, store_id=run["store_id"], sku_id=run["sku_id"]
        )
        balance = next((item for item in balances if item["warehouse_id"] == warehouse_id), None)
        if balance is None:
            raise ValueError("inventory_balance_not_found")
        points = run["forecast_points"]
        planning_days = policy["supplier_lead_days"] + policy["review_period_days"]
        if planning_days < 1 or len(points) < planning_days:
            raise ValueError("forecast_horizon_insufficient")
        selected = [Decimal(str(item[policy["service_level"]])) for item in points]
        lead_demand = sum(selected[: policy["supplier_lead_days"]], Decimal("0"))
        target_demand = sum(selected[:planning_days], Decimal("0"))
        average_daily = target_demand / Decimal(planning_days)
        target_stock = max(
            target_demand,
            Decimal(str(policy["minimum_safety_stock"])),
        )
        maximum_stock = None
        if policy["maximum_stock_days"] is not None:
            maximum_stock = average_daily * Decimal(str(policy["maximum_stock_days"]))
            target_stock = min(target_stock, maximum_stock)
        on_hand = Decimal(str(balance["on_hand"]))
        reserved = Decimal(str(balance["reserved"]))
        inbound = Decimal(str(balance["inbound"]))
        available = max(Decimal("0"), on_hand - reserved)
        raw_quantity = max(Decimal("0"), target_stock - available - inbound)
        recommended = self._round_quantity(
            raw_quantity,
            minimum=Decimal(str(policy["minimum_order_qty"])),
            multiple=Decimal(str(policy["order_multiple"])),
        )
        expected_stockout = self._stockout_date(
            points,
            service_level=policy["service_level"],
            supply=available + inbound,
        )
        risk_level = self._risk_level(
            expected_stockout,
            training_end=run["training_end"],
            lead_days=policy["supplier_lead_days"],
            review_days=policy["review_period_days"],
            recommended=recommended,
        )
        snapshot = {
            "balance_id": balance["id"],
            "on_hand": balance["on_hand"],
            "reserved": balance["reserved"],
            "inbound": balance["inbound"],
            "available": self._decimal(available),
            "source_updated_at": balance["source_updated_at"],
        }
        snapshot_hash = payload_digest(snapshot)
        plan_id = f"inventory-plan-{uuid.uuid4().hex}"
        explanation = {
            "warehouse_scope": "supply_location_only",
            "demand_scope": "store_sku",
            "store_total_demand_calculated_once": True,
            "warehouse_allocation": "caller_allocated",
            "service_level": policy["service_level"],
            "lead_demand": self._decimal(lead_demand),
            "target_demand": self._decimal(target_demand),
            "average_daily_demand": self._decimal(average_daily),
            "maximum_stock": self._decimal(maximum_stock),
            "calculation": "available + inbound -> target stock -> safety stock -> MOQ -> multiple",
        }
        rounding = {
            "minimum_order_qty": self._decimal(Decimal(str(policy["minimum_order_qty"]))),
            "order_multiple": self._decimal(Decimal(str(policy["order_multiple"]))),
            "minimum_safety_stock": self._decimal(
                Decimal(str(policy["minimum_safety_stock"]))
            ),
            "raw_order_qty": self._decimal(raw_quantity),
        }
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO inventory_plans(
                    plan_id, tenant_id, store_id, sku_id, warehouse_id,
                    forecast_run_id, policy_id, policy_version, inventory_snapshot_hash,
                    inventory_snapshot_json, inbound, reorder_point, target_stock,
                    recommended_order_qty, expected_stockout_date, risk_level,
                    rounding_json, explanation_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    plan_id,
                    tenant_id,
                    run["store_id"],
                    run["sku_id"],
                    warehouse_id,
                    forecast_run_id,
                    policy["policy_id"],
                    policy["policy_version"],
                    snapshot_hash,
                    json.dumps(snapshot, sort_keys=True),
                    self._decimal(inbound),
                    self._decimal(lead_demand),
                    self._decimal(target_stock),
                    self._decimal(recommended),
                    expected_stockout,
                    risk_level,
                    json.dumps(rounding, sort_keys=True),
                    json.dumps(explanation, sort_keys=True),
                    utc_now(),
                ),
            )
        return self.get_plan(tenant_id, plan_id)

    def get_plan(self, tenant_id: str, plan_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_plans WHERE tenant_id=? AND plan_id=?",
                (tenant_id, plan_id),
            ).fetchone()
        if row is None:
            raise ValueError("inventory_plan_not_found")
        return self._view(dict(row))

    def latest_plan(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        warehouse_id: str,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM inventory_plans
                WHERE tenant_id=? AND store_id=? AND sku_id=? AND warehouse_id=?
                ORDER BY created_at DESC, plan_id DESC LIMIT 1
                """,
                (tenant_id, store_id, sku_id, warehouse_id),
            ).fetchone()
        return self._view(dict(row)) if row else None

    def list_risks(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        warehouse_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if warehouse_id:
            conditions.append("warehouse_id=?")
            params.append(warehouse_id)
        if risk_level:
            conditions.append("risk_level=?")
            params.append(risk_level)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM inventory_plans
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE risk_level
                    WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 WHEN 'replenishment_due' THEN 3 ELSE 4 END,
                    created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    def latest_policy(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        warehouse_id: str,
    ) -> dict[str, Any] | None:
        try:
            policy = self._select_policy(
                tenant_id, store_id=store_id, sku_id=sku_id, warehouse_id=warehouse_id
            )
        except ValueError:
            return None
        return self._policy_view(tenant_id, str(policy["policy_id"]))

    def list_plans(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
        warehouse_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        for field, value in (
            ("store_id", store_id),
            ("sku_id", sku_id),
            ("warehouse_id", warehouse_id),
            ("risk_level", risk_level),
        ):
            if value:
                conditions.append(f"{field}=?")
                params.append(value)
        params.append(max(1, min(100, limit)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM inventory_plans WHERE {' AND '.join(conditions)} "
                "ORDER BY created_at DESC, plan_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    def _select_policy(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        warehouse_id: str,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM inventory_planning_policies
                WHERE tenant_id=? AND store_id=? AND sku_id=?
                  AND (warehouse_id=? OR warehouse_id IS NULL)
                ORDER BY CASE WHEN warehouse_id=? THEN 0 ELSE 1 END,
                         active_from DESC, policy_version DESC
                LIMIT 1
                """,
                (tenant_id, store_id, sku_id, warehouse_id, warehouse_id),
            ).fetchone()
        if row is None:
            raise ValueError("inventory_policy_not_found")
        return dict(row)

    def _policy_view(self, tenant_id: str, policy_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_planning_policies WHERE tenant_id=? AND policy_id=?",
                (tenant_id, policy_id),
            ).fetchone()
        if row is None:
            raise ValueError("inventory_policy_not_found")
        return dict(row)

    @staticmethod
    def _policy_id(tenant_id: str, policy: InventoryPlanningPolicy) -> str:
        warehouse = policy.warehouse_id or "default"
        return f"inventory-policy-{tenant_id}-{policy.store_id}-{policy.sku_id}-{warehouse}"

    @staticmethod
    def _round_quantity(value: Decimal, *, minimum: Decimal, multiple: Decimal) -> Decimal:
        if value <= 0:
            return Decimal("0")
        rounded = max(value, minimum)
        return (rounded / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple

    @staticmethod
    def _stockout_date(
        points: list[dict[str, Any]], *, service_level: ServiceLevel, supply: Decimal
    ) -> str | None:
        remaining = supply
        for point in points:
            remaining -= Decimal(str(point[service_level]))
            if remaining < 0:
                return str(point["forecast_date"])
        return None

    @staticmethod
    def _risk_level(
        expected_stockout: str | None,
        *,
        training_end: str,
        lead_days: int,
        review_days: int,
        recommended: Decimal,
    ) -> str:
        if expected_stockout is None:
            return "replenishment_due" if recommended > 0 else "healthy"
        stockout = date.fromisoformat(expected_stockout)
        end = date.fromisoformat(training_end)
        days = (stockout - end).days
        if days <= lead_days:
            return "critical"
        if days <= lead_days + review_days:
            return "high"
        return "medium"

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        return format(value.quantize(Decimal("0.01")), "f") if value is not None else None

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        snapshot = json.loads(row["inventory_snapshot_json"])
        rounding = json.loads(row["rounding_json"])
        return {
            "plan_id": row["plan_id"],
            "tenant_id": row["tenant_id"],
            "store_id": row["store_id"],
            "sku_id": row["sku_id"],
            "warehouse_id": row["warehouse_id"],
            "forecast_run_id": row["forecast_run_id"],
            "policy_id": row["policy_id"],
            "policy_version": row["policy_version"],
            "inventory_snapshot": snapshot,
            "inbound": row["inbound"],
            "reorder_point": row["reorder_point"],
            "target_stock": row["target_stock"],
            "recommended_order_qty": row["recommended_order_qty"],
            "expected_stockout_date": row["expected_stockout_date"],
            "risk_level": row["risk_level"],
            "rounding": rounding,
            "calculation": {
                "available": snapshot["available"],
                "inbound": row["inbound"],
                "reorder_point": row["reorder_point"],
                "target_stock": row["target_stock"],
                "minimum_safety_stock": rounding["minimum_safety_stock"],
                "raw_order_qty": rounding["raw_order_qty"],
            },
            "explanation": json.loads(row["explanation_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }
