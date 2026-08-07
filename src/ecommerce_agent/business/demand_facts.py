from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..database import Database, utc_now
from .inventory import InventoryService
from .orders import OrderService
from .source_versioning import SourceVersionError, payload_digest


class DemandPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = "demand-v1"
    timezone: str = "Asia/Shanghai"
    included_payment_statuses: tuple[str, ...] = ("paid", "partially_refunded")
    excluded_order_statuses: tuple[str, ...] = ("canceled",)
    late_arrival_policy: Literal["bounded_lookback"] = "bounded_lookback"
    rebuild_lookback_days: int = Field(default=56, ge=1, le=3650)
    missing_date_policy: Literal["fill_zero_with_evidence"] = "fill_zero_with_evidence"


class DemandFactRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    start_date: date
    end_date: date
    mode: Literal["full", "incremental"] = "incremental"
    source_gap_dates: list[date] = Field(default_factory=list, max_length=3650)

    @model_validator(mode="after")
    def date_range_is_valid(self) -> "DemandFactRebuildRequest":
        if self.end_date < self.start_date:
            raise ValueError("demand_fact_invalid_date_range")
        return self


class DemandFactService:
    def __init__(
        self,
        db: Database,
        *,
        orders: OrderService,
        inventory: InventoryService,
        policy: DemandPolicy | None = None,
    ):
        self.db = db
        self.orders = orders
        self.inventory = inventory
        self.policy = policy or DemandPolicy()
        self.zone = timezone(timedelta(hours=8))

    def rebuild(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        start_date: date,
        end_date: date,
        mode: str = "incremental",
        source_gap_dates: list[date] | None = None,
    ) -> dict[str, Any]:
        if end_date < start_date:
            raise ValueError("demand_fact_invalid_date_range")
        if mode not in {"full", "incremental"}:
            raise ValueError("demand_fact_invalid_mode")
        source_gap_set = set(source_gap_dates or [])
        if any(item < start_date or item > end_date for item in source_gap_set):
            raise ValueError("demand_fact_source_gap_outside_range")

        orders = self.orders.list_orders(tenant_id, store_id=store_id, limit=100000)
        daily: dict[date, dict[str, Any]] = {}
        source_times: list[str] = []
        for order in orders:
            business_day = self._business_date(str(order["placed_at"]))
            if business_day < start_date or business_day > end_date:
                continue
            matching_lines = [
                line for line in order["lines"] if str(line["sku_id"]) == sku_id
            ]
            if not matching_lines:
                continue
            source_times.append(str(order["source_updated_at"]))
            bucket = daily.setdefault(
                business_day,
                {
                    "gross_units": Decimal("0"),
                    "eligible_units": Decimal("0"),
                    "order_ids": set(),
                    "sales_amount": Decimal("0"),
                    "price_amount": Decimal("0"),
                },
            )
            bucket["order_ids"].add(str(order["order_id"]))
            eligible = (
                str(order["order_status"]) not in self.policy.excluded_order_statuses
                and str(order["payment_status"]) in self.policy.included_payment_statuses
            )
            for line in matching_lines:
                quantity = Decimal(str(line["quantity"]))
                bucket["gross_units"] += quantity
                if eligible:
                    unit_price = Decimal(str(line["unit_price"]))
                    bucket["eligible_units"] += quantity
                    bucket["sales_amount"] += quantity * unit_price
                    bucket["price_amount"] += unit_price * quantity

        if not daily:
            raise ValueError("demand_no_source_data")

        balances = self.inventory.list_balances(tenant_id, store_id=store_id, sku_id=sku_id)
        available_stock, inventory_watermark = self._inventory_snapshot(balances)
        order_watermark = max(source_times)
        source_watermark = max(
            (value for value in (order_watermark, inventory_watermark) if value),
            default=order_watermark,
        )
        facts: list[dict[str, Any]] = []
        cursor = start_date
        while cursor <= end_date:
            bucket = daily.get(cursor)
            has_source = bucket is not None
            bucket = bucket or {
                "gross_units": Decimal("0"),
                "eligible_units": Decimal("0"),
                "order_ids": set(),
                "sales_amount": Decimal("0"),
                "price_amount": Decimal("0"),
            }
            eligible_units = bucket["eligible_units"]
            stockout_flag = "unknown"
            is_source_gap = cursor in source_gap_set
            if is_source_gap:
                demand_state = "source_gap"
            elif has_source:
                demand_state = "observed_demand" if eligible_units > 0 else "observed_zero"
            else:
                demand_state = "no_orders"
            evidence = {
                "missing_source_date": is_source_gap,
                "source_gap": is_source_gap,
                "demand_state": demand_state,
                "inventory_balance_count": len(balances),
                "inventory_watermark": inventory_watermark,
                "stockout_reason": (
                    "no_historical_inventory_snapshot"
                    if balances
                    else "inventory_snapshot_unavailable"
                ),
            }
            price = (
                bucket["price_amount"] / eligible_units
                if eligible_units > 0
                else None
            )
            facts.append(
                {
                    "business_date": cursor.isoformat(),
                    "gross_units": self._decimal(bucket["gross_units"]),
                    "eligible_units": self._decimal(eligible_units),
                    "order_count": len(bucket["order_ids"]),
                    "sales_amount": self._decimal(bucket["sales_amount"]),
                    "available_stock": (
                        self._decimal(available_stock) if available_stock is not None else None
                    ),
                    "stockout_flag": stockout_flag,
                    "stockout_evidence": evidence,
                    "price": self._decimal(price) if price is not None else None,
                    "promotion_flag": None,
                    "source_watermark": source_watermark,
                    "fact_version": 0,
                    "demand_policy_version": self.policy.policy_version,
                }
            )
            cursor += timedelta(days=1)

        write_status = "applied"
        fact_version = 1
        with self.db._write_lock, self.db.connect() as conn:
            latest = conn.execute(
                """
                SELECT MAX(fact_version) AS fact_version, MAX(source_watermark) AS source_watermark
                FROM demand_daily_facts
                WHERE tenant_id=? AND store_id=? AND sku_id=? AND demand_policy_version=?
                """,
                (tenant_id, store_id, sku_id, self.policy.policy_version),
            ).fetchone()
            latest_version = int(latest["fact_version"] or 0)
            latest_watermark = latest["source_watermark"]
            fact_version = latest_version + 1
            if latest_version and latest_watermark:
                if source_watermark < str(latest_watermark):
                    raise SourceVersionError("demand_fact_stale_watermark")
                if source_watermark == str(latest_watermark):
                    payloads = {
                        item["business_date"]: payload_digest(
                            {**item, "fact_version": latest_version}
                        )
                        for item in facts
                    }
                    existing = conn.execute(
                        """
                        SELECT business_date, payload_hash
                        FROM demand_daily_facts
                        WHERE tenant_id=? AND store_id=? AND sku_id=?
                          AND demand_policy_version=? AND fact_version=?
                        """,
                        (
                            tenant_id,
                            store_id,
                            sku_id,
                            self.policy.policy_version,
                            latest_version,
                        ),
                    ).fetchall()
                    existing_hashes = {str(row["business_date"]): str(row["payload_hash"]) for row in existing}
                    if existing_hashes == payloads:
                        write_status = "idempotent"
                        fact_version = latest_version
                    else:
                        raise SourceVersionError("demand_fact_source_version_conflict")
            if write_status == "applied":
                now = utc_now()
                for item in facts:
                    item["fact_version"] = fact_version
                    payload_hash = payload_digest(item)
                    conn.execute(
                        """
                        INSERT INTO demand_daily_facts(
                            id, tenant_id, store_id, sku_id, business_date,
                            gross_units, eligible_units, order_count, sales_amount,
                            available_stock, stockout_flag, stockout_evidence, price,
                            promotion_flag, source_watermark, fact_version,
                            demand_policy_version, payload_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"demand-fact-{uuid.uuid4().hex}",
                            tenant_id,
                            store_id,
                            sku_id,
                            item["business_date"],
                            item["gross_units"],
                            item["eligible_units"],
                            item["order_count"],
                            item["sales_amount"],
                            item["available_stock"],
                            item["stockout_flag"],
                            json.dumps(item["stockout_evidence"], sort_keys=True),
                            item["price"],
                            item["promotion_flag"],
                            item["source_watermark"],
                            fact_version,
                            item["demand_policy_version"],
                            payload_hash,
                            now,
                        ),
                    )

        return {
            "fact_version": fact_version,
            "write_status": write_status,
            "source_watermark": source_watermark,
            "facts": self.list_facts(tenant_id, store_id=store_id, sku_id=sku_id),
            "quality": self._quality(facts),
        }

    def list_facts(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?", "store_id=?", "sku_id=?"]
        params: list[Any] = [tenant_id, store_id, sku_id]
        if start_date is not None:
            conditions.append("business_date>=?")
            params.append(start_date.isoformat())
        if end_date is not None:
            conditions.append("business_date<=?")
            params.append(end_date.isoformat())
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT f.* FROM demand_daily_facts f
                WHERE {' AND '.join(conditions)}
                  AND f.fact_version=(
                    SELECT MAX(latest.fact_version) FROM demand_daily_facts latest
                    WHERE latest.tenant_id=f.tenant_id
                      AND latest.store_id=f.store_id
                      AND latest.sku_id=f.sku_id
                      AND latest.business_date=f.business_date
                      AND latest.demand_policy_version=f.demand_policy_version
                  )
                ORDER BY business_date
                """,
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    @staticmethod
    def _inventory_snapshot(
        balances: list[dict[str, Any]],
    ) -> tuple[Decimal | None, str | None]:
        if not balances:
            return None, None
        available = Decimal("0")
        watermarks: list[str] = []
        for balance in balances:
            available += max(
                Decimal("0"),
                Decimal(str(balance["on_hand"])) - Decimal(str(balance["reserved"])),
            )
            if balance.get("source_updated_at"):
                watermarks.append(str(balance["source_updated_at"]))
        return available, max(watermarks) if watermarks else None

    def _quality(self, facts: list[dict[str, Any]]) -> dict[str, Any]:
        if not facts:
            return {
                "level": "unknown",
                "missing_source_dates": 0,
                "unknown_stockout_dates": 0,
                "demand_policy_version": self.policy.policy_version,
                "reason": "no_demand_facts",
            }
        missing = sum(bool(item["stockout_evidence"].get("source_gap")) for item in facts)
        unknown = sum(item["stockout_flag"] == "unknown" for item in facts)
        return {
            "level": "degraded" if missing or unknown else "good",
            "missing_source_dates": missing,
            "unknown_stockout_dates": unknown,
            "demand_policy_version": self.policy.policy_version,
        }

    def _business_date(self, value: str) -> date:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(self.zone).date()

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value.quantize(Decimal("0.01")), "f")

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "store_id": row["store_id"],
            "sku_id": row["sku_id"],
            "business_date": row["business_date"],
            "gross_units": row["gross_units"],
            "eligible_units": row["eligible_units"],
            "order_count": row["order_count"],
            "sales_amount": row["sales_amount"],
            "available_stock": row["available_stock"],
            "stockout_flag": row["stockout_flag"],
            "stockout_evidence": json.loads(row["stockout_evidence"]),
            "price": row["price"],
            "promotion_flag": row["promotion_flag"],
            "source_watermark": row["source_watermark"],
            "fact_version": row["fact_version"],
            "demand_policy_version": row["demand_policy_version"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
            "quality": {
                "missing_source_date": bool(
                    json.loads(row["stockout_evidence"]).get("source_gap")
                )
            },
        }
