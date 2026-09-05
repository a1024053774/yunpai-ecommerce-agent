from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..database import Database, utc_now
from .source_versioning import SourceVersionError, canonical_source_time, decide_write


AvailabilityScope = Literal["product", "sku"]
CHANNEL_AVAILABLE_ROLE = "channel_available"


def _quantity_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class ChannelAvailabilityRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: AvailabilityScope
    source_sku_id: str | None = Field(default=None, min_length=1, max_length=128)
    warehouse_code: str | None = Field(default=None, min_length=1, max_length=128)
    available_qty: Decimal = Field(ge=0)

    @field_validator("source_sku_id", "warehouse_code")
    @classmethod
    def normalize_optional_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("available_qty")
    @classmethod
    def require_finite_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("channel_available_quantity_invalid")
        return value

    @model_validator(mode="after")
    def validate_scope_identity(self) -> "ChannelAvailabilityRecordInput":
        if self.scope == "product" and self.source_sku_id is not None:
            raise ValueError("product_scope_must_not_have_sku")
        if self.scope == "sku" and not self.source_sku_id:
            raise ValueError("sku_scope_requires_sku")
        return self


class ChannelAvailabilitySnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    source_product_id: str = Field(min_length=1, max_length=128)
    source_updated_at: datetime
    payload_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    source_id: str | None = Field(default=None, max_length=256)
    observed_at: datetime | None = None
    unit: str | None = Field(default=None, max_length=64)
    inventory_reduce_type: str | None = Field(default=None, max_length=64)
    records: list[ChannelAvailabilityRecordInput] = Field(min_length=1, max_length=10000)

    @field_validator("connector_id", "store_id", "source_product_id")
    @classmethod
    def normalize_required_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("channel_availability_identifier_required")
        return normalized

    @field_validator("source_updated_at", "observed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            canonical_source_time(value)
        return value

    @field_validator("payload_hash")
    @classmethod
    def normalize_payload_hash(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def reject_duplicate_records(self) -> "ChannelAvailabilitySnapshotInput":
        keys = [
            (record.scope, record.source_sku_id, record.warehouse_code)
            for record in self.records
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_channel_availability_record")
        if sum(record.scope == "product" for record in self.records) > 1:
            raise ValueError("duplicate_product_channel_availability_record")
        return self


class ChannelAvailabilityService:
    """Persist the current channel-available fact without changing WMS balances."""

    def __init__(self, db: Database):
        self.db = db

    def replace_snapshot(
        self, tenant_id: str, value: ChannelAvailabilitySnapshotInput
    ) -> dict[str, Any]:
        if not tenant_id.strip():
            raise ValueError("tenant_id_required")
        source_time = canonical_source_time(value.source_updated_at)
        observed_at = (
            canonical_source_time(value.observed_at)
            if value.observed_at is not None
            else utc_now()
        )
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.replace_snapshot_on_conn(
                conn,
                tenant_id,
                value,
                source_time=source_time,
                observed_at=observed_at,
                now=now,
            )

    def replace_snapshot_on_conn(
        self,
        conn: Any,
        tenant_id: str,
        value: ChannelAvailabilitySnapshotInput,
        *,
        source_time: str | None = None,
        observed_at: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        if not tenant_id.strip():
            raise ValueError("tenant_id_required")
        resolved_source = source_time or canonical_source_time(value.source_updated_at)
        resolved_observed = observed_at or (
            canonical_source_time(value.observed_at)
            if value.observed_at is not None
            else utc_now()
        )
        resolved_now = now or utc_now()
        write_status = "applied"
        existing = conn.execute(
            """
            SELECT snapshot_id, source_updated_at, payload_hash, version
            FROM channel_availability_snapshots
            WHERE tenant_id=? AND connector_id=? AND store_id=?
              AND source_product_id=?
            """,
            (
                tenant_id,
                value.connector_id,
                value.store_id,
                value.source_product_id,
            ),
        ).fetchone()
        if existing is not None:
            decision = decide_write(
                existing_source_time=str(existing["source_updated_at"]),
                existing_payload_hash=str(existing["payload_hash"]),
                incoming_source_time=resolved_source,
                incoming_payload_hash=value.payload_hash,
            )
            snapshot_id = str(existing["snapshot_id"])
            if decision == "idempotent":
                write_status = "idempotent"
        else:
            snapshot_id = f"availability-snapshot-{uuid.uuid4().hex}"

        if write_status == "applied":
            version = int(existing["version"]) + 1 if existing else 1
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO channel_availability_snapshots(
                        snapshot_id, tenant_id, connector_id, store_id,
                        source_product_id, semantic_role, unit,
                        inventory_reduce_type, source_id, source_updated_at,
                        observed_at, payload_hash, record_count, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id, tenant_id, value.connector_id, value.store_id,
                        value.source_product_id, CHANNEL_AVAILABLE_ROLE, value.unit,
                        value.inventory_reduce_type, value.source_id, resolved_source,
                        resolved_observed, value.payload_hash, len(value.records),
                        version, resolved_now, resolved_now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE channel_availability_snapshots SET
                        semantic_role=?, unit=?, inventory_reduce_type=?,
                        source_id=?, source_updated_at=?, observed_at=?,
                        payload_hash=?, record_count=?, version=?, updated_at=?
                    WHERE tenant_id=? AND snapshot_id=?
                    """,
                    (
                        CHANNEL_AVAILABLE_ROLE, value.unit,
                        value.inventory_reduce_type, value.source_id,
                        resolved_source, resolved_observed, value.payload_hash,
                        len(value.records), version, resolved_now,
                        tenant_id, snapshot_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM channel_availability_records WHERE tenant_id=? AND snapshot_id=?",
                    (tenant_id, snapshot_id),
                )
            for record in value.records:
                conn.execute(
                    """
                    INSERT INTO channel_availability_records(
                        record_id, tenant_id, snapshot_id, connector_id, store_id,
                        source_product_id, semantic_role, scope, source_sku_id,
                        warehouse_code, available_qty, source_updated_at,
                        payload_hash, version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"availability-record-{uuid.uuid4().hex}", tenant_id,
                        snapshot_id, value.connector_id, value.store_id,
                        value.source_product_id, CHANNEL_AVAILABLE_ROLE, record.scope,
                        record.source_sku_id, record.warehouse_code,
                        _quantity_text(record.available_qty), resolved_source,
                        value.payload_hash, version, resolved_now,
                    ),
                )

        result = self._snapshot_view(conn, tenant_id, snapshot_id)
        result["write_status"] = write_status
        return result

    def get_snapshot(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        store_id: str,
        source_product_id: str,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT snapshot_id FROM channel_availability_snapshots
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND source_product_id=?
                """,
                (tenant_id, connector_id, store_id, source_product_id),
            ).fetchone()
            if row is None:
                return None
            return self._snapshot_view(conn, tenant_id, str(row["snapshot_id"]))

    def count_snapshots(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        store_id: str,
    ) -> int:
        if not tenant_id.strip() or not connector_id.strip() or not store_id.strip():
            raise ValueError("channel_availability_identifier_required")
        with self.db.connect() as conn:
            return self.count_snapshots_on_conn(
                conn,
                tenant_id,
                connector_id=connector_id,
                store_id=store_id,
            )

    @staticmethod
    def count_snapshots_on_conn(
        conn: Any,
        tenant_id: str,
        *,
        connector_id: str,
        store_id: str,
    ) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM channel_availability_snapshots
            WHERE tenant_id=? AND connector_id=? AND store_id=?
            """,
            (tenant_id, connector_id, store_id),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def list_current(
        self,
        tenant_id: str,
        *,
        connector_id: str | None = None,
        store_id: str | None = None,
        source_product_id: str | None = None,
        source_sku_id: str | None = None,
        scope: AvailabilityScope | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("channel_availability_limit_invalid")
        if scope is not None and scope not in {"product", "sku"}:
            raise ValueError("channel_availability_scope_invalid")
        conditions = ["r.tenant_id=?"]
        params: list[Any] = [tenant_id]
        for column, value in (
            ("r.connector_id", connector_id),
            ("r.store_id", store_id),
            ("r.source_product_id", source_product_id),
            ("r.source_sku_id", source_sku_id),
            ("r.scope", scope),
        ):
            if value is not None:
                conditions.append(f"{column}=?")
                params.append(value)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    r.record_id, r.tenant_id, r.snapshot_id, r.connector_id,
                    r.store_id, r.source_product_id, r.semantic_role, r.scope,
                    r.source_sku_id, r.warehouse_code, r.available_qty,
                    s.unit, s.inventory_reduce_type, s.source_id,
                    r.source_updated_at, s.observed_at, r.payload_hash,
                    r.version, s.record_count, s.created_at, s.updated_at
                FROM channel_availability_records r
                JOIN channel_availability_snapshots s
                  ON s.tenant_id=r.tenant_id AND s.snapshot_id=r.snapshot_id
                WHERE {' AND '.join(conditions)}
                ORDER BY r.store_id, r.source_product_id,
                         CASE WHEN r.scope='product' THEN 0 ELSE 1 END,
                         r.source_sku_id
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._record_view(dict(row)) for row in rows]

    @classmethod
    def _snapshot_view(
        cls, conn: Any, tenant_id: str, snapshot_id: str
    ) -> dict[str, Any]:
        snapshot = conn.execute(
            "SELECT * FROM channel_availability_snapshots WHERE tenant_id=? AND snapshot_id=?",
            (tenant_id, snapshot_id),
        ).fetchone()
        if snapshot is None:
            raise ValueError("channel_availability_snapshot_not_found")
        records = conn.execute(
            """
            SELECT record_id, connector_id, store_id, source_product_id,
                   semantic_role, scope, source_sku_id, warehouse_code,
                   available_qty, source_updated_at, payload_hash, version, created_at
            FROM channel_availability_records
            WHERE tenant_id=? AND snapshot_id=?
            ORDER BY CASE WHEN scope='product' THEN 0 ELSE 1 END, source_sku_id
            """,
            (tenant_id, snapshot_id),
        ).fetchall()
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "connector_id": snapshot["connector_id"],
            "store_id": snapshot["store_id"],
            "source_product_id": snapshot["source_product_id"],
            "semantic_role": snapshot["semantic_role"],
            "unit": snapshot["unit"],
            "inventory_reduce_type": snapshot["inventory_reduce_type"],
            "source_id": snapshot["source_id"],
            "source_updated_at": snapshot["source_updated_at"],
            "observed_at": snapshot["observed_at"],
            "payload_hash": snapshot["payload_hash"],
            "record_count": snapshot["record_count"],
            "version": snapshot["version"],
            "created_at": snapshot["created_at"],
            "updated_at": snapshot["updated_at"],
            "records": [cls._record_view(dict(row)) for row in records],
        }

    @staticmethod
    def _record_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "record_id",
                "connector_id",
                "store_id",
                "source_product_id",
                "semantic_role",
                "scope",
                "source_sku_id",
                "warehouse_code",
                "available_qty",
                "unit",
                "inventory_reduce_type",
                "source_id",
                "source_updated_at",
                "observed_at",
                "payload_hash",
                "version",
                "record_count",
                "created_at",
                "updated_at",
            )
            if key in row
        }


__all__ = [
    "AvailabilityScope",
    "CHANNEL_AVAILABLE_ROLE",
    "ChannelAvailabilityRecordInput",
    "ChannelAvailabilityService",
    "ChannelAvailabilitySnapshotInput",
    "SourceVersionError",
]
