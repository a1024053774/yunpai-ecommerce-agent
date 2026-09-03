from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from ..database import Database, utc_now


AVAILABILITY_RESOURCE = "channel_availability"
PAGE_RETRY_LIMIT = 3
LEASE_SECONDS = 300
WINDOW_FULL = "full"
WINDOW_INCREMENTAL = "incremental"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_COMPLETE = "complete"
WATERMARK_SOURCE_TIME = "source_time"
WATERMARK_EXHAUSTED = "exhausted"

_IDENTITY_COLUMNS = (
    "tenant_id",
    "connector_id",
    "store_id",
    "resource",
    "window_kind",
    "window_start",
    "window_end",
)


class SyncCheckpointConflict(ValueError):
    """Another owner still holds a live lease on this checkpoint."""


def availability_window(
    modify_start_time: str | None,
    modify_end_time: str | None,
) -> tuple[str, str, str]:
    start = (modify_start_time or "").strip()
    end = (modify_end_time or "").strip()
    if start or end:
        return WINDOW_INCREMENTAL, start, end
    return WINDOW_FULL, "", ""


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _lease_deadline(now: str, seconds: int = LEASE_SECONDS) -> str:
    return (_as_datetime(now) + timedelta(seconds=seconds)).isoformat()


def _lease_is_live(row: Any, now: str, owner: str) -> bool:
    held_by = row["lease_owner"]
    expires_at = row["lease_expires_at"]
    if not held_by or not expires_at:
        return False
    if held_by == owner:
        return False
    return _as_datetime(expires_at) > _as_datetime(now)


def public_checkpoint_view(row: Any) -> dict[str, Any]:
    return {
        "status": row["status"],
        "cursor": row["cursor"] or None,
        "window_kind": row["window_kind"],
        "window_start": row["window_start"] or None,
        "window_end": row["window_end"] or None,
        "pages_completed": int(row["pages_completed"]),
        "upstream_total": row["upstream_total"],
        "watermark": row["watermark"],
        "watermark_kind": row["watermark_kind"],
        "last_error_kind": row["last_error_kind"],
    }


class ConnectorSyncCheckpointService:
    """Durable per-store, per-window connector sync checkpoint."""

    def __init__(self, db: Database):
        self.db = db

    def acquire(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        store_id: str,
        resource: str,
        window_kind: str,
        window_start: str,
        window_end: str,
        owner: str,
        requested_cursor: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        identity = (
            tenant_id,
            connector_id,
            store_id,
            resource,
            window_kind,
            window_start,
            window_end,
        )
        requested = (requested_cursor or "").strip()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._get(conn, identity)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO connector_sync_checkpoints(
                        checkpoint_id, tenant_id, connector_id, store_id,
                        resource, window_kind, window_start, window_end,
                        status, cursor, watermark, watermark_kind,
                        upstream_total, pages_completed, records_received,
                        records_applied, last_error, last_error_kind,
                        lease_owner, lease_expires_at, row_version,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                        NULL, 0, 0, 0, NULL, NULL, ?, ?, 1, ?, ?
                    )
                    """,
                    (
                        f"sync-checkpoint-{uuid.uuid4().hex}",
                        *identity,
                        STATUS_RUNNING,
                        requested,
                        owner,
                        _lease_deadline(now),
                        now,
                        now,
                    ),
                )
                return dict(self._get(conn, identity))
            if _lease_is_live(existing, now, owner):
                raise SyncCheckpointConflict(
                    "1688 availability sync is already running for this store window"
                )
            restart = existing["status"] == STATUS_COMPLETE
            cursor = requested if restart else str(existing["cursor"] or "")
            updated = conn.execute(
                """
                UPDATE connector_sync_checkpoints SET
                    status=?,
                    cursor=?,
                    watermark=CASE WHEN ? THEN NULL ELSE watermark END,
                    watermark_kind=CASE WHEN ? THEN NULL ELSE watermark_kind END,
                    upstream_total=CASE WHEN ? THEN NULL ELSE upstream_total END,
                    pages_completed=CASE WHEN ? THEN 0 ELSE pages_completed END,
                    records_received=CASE WHEN ? THEN 0 ELSE records_received END,
                    records_applied=CASE WHEN ? THEN 0 ELSE records_applied END,
                    last_error=NULL,
                    last_error_kind=NULL,
                    lease_owner=?,
                    lease_expires_at=?,
                    row_version=row_version + 1,
                    updated_at=?
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND resource=? AND window_kind=? AND window_start=?
                  AND window_end=?
                  AND row_version=?
                  AND (
                    lease_owner IS NULL
                    OR lease_owner=?
                    OR lease_expires_at IS NULL
                    OR lease_expires_at<=?
                  )
                """,
                (
                    STATUS_RUNNING,
                    cursor,
                    restart,
                    restart,
                    restart,
                    restart,
                    restart,
                    restart,
                    owner,
                    _lease_deadline(now),
                    now,
                    *identity,
                    int(existing["row_version"]),
                    owner,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise SyncCheckpointConflict(
                    "1688 availability sync is already running for this store window"
                )
            return dict(self._get(conn, identity))

    def apply_page(
        self,
        conn: Any,
        row: dict[str, Any],
        *,
        owner: str,
        cursor: str,
        pages_completed: int,
        records_received: int,
        records_applied: int,
        upstream_total: int | None,
        expected_version: int,
    ) -> dict[str, Any]:
        return self._update(
            conn,
            row,
            owner=owner,
            expected_version=expected_version,
            status=STATUS_RUNNING,
            cursor=cursor,
            pages_completed=pages_completed,
            records_received=records_received,
            records_applied=records_applied,
            upstream_total=upstream_total,
            watermark=row.get("watermark"),
            watermark_kind=row.get("watermark_kind"),
            last_error=None,
            last_error_kind=None,
            clear_lease=False,
        )

    def complete(
        self,
        conn: Any,
        row: dict[str, Any],
        *,
        owner: str,
        expected_version: int,
        cursor: str,
        pages_completed: int,
        records_received: int,
        records_applied: int,
        upstream_total: int | None,
        watermark: str | None,
        watermark_kind: str,
    ) -> dict[str, Any]:
        return self._update(
            conn,
            row,
            owner=owner,
            expected_version=expected_version,
            status=STATUS_COMPLETE,
            cursor=cursor,
            pages_completed=pages_completed,
            records_received=records_received,
            records_applied=records_applied,
            upstream_total=upstream_total,
            watermark=watermark,
            watermark_kind=watermark_kind,
            last_error=None,
            last_error_kind=None,
            clear_lease=True,
        )

    def fail(
        self,
        row: dict[str, Any],
        *,
        owner: str,
        expected_version: int,
        error_kind: str,
        error: str,
    ) -> dict[str, Any]:
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._update(
                conn,
                row,
                owner=owner,
                expected_version=expected_version,
                status=STATUS_FAILED,
                cursor=str(row.get("cursor") or ""),
                pages_completed=int(row.get("pages_completed") or 0),
                records_received=int(row.get("records_received") or 0),
                records_applied=int(row.get("records_applied") or 0),
                upstream_total=row.get("upstream_total"),
                watermark=row.get("watermark"),
                watermark_kind=row.get("watermark_kind"),
                last_error=error,
                last_error_kind=error_kind,
                clear_lease=True,
            )

    def fail_on_conn(
        self,
        conn: Any,
        row: dict[str, Any],
        *,
        owner: str,
        expected_version: int,
        error_kind: str,
        error: str,
    ) -> dict[str, Any]:
        return self._update(
            conn,
            row,
            owner=owner,
            expected_version=expected_version,
            status=STATUS_FAILED,
            cursor=str(row.get("cursor") or ""),
            pages_completed=int(row.get("pages_completed") or 0),
            records_received=int(row.get("records_received") or 0),
            records_applied=int(row.get("records_applied") or 0),
            upstream_total=row.get("upstream_total"),
            watermark=row.get("watermark"),
            watermark_kind=row.get("watermark_kind"),
            last_error=error,
            last_error_kind=error_kind,
            clear_lease=True,
        )

    def _update(
        self,
        conn: Any,
        row: dict[str, Any],
        *,
        owner: str,
        expected_version: int,
        status: str,
        cursor: str,
        pages_completed: int,
        records_received: int,
        records_applied: int,
        upstream_total: int | None,
        watermark: str | None,
        watermark_kind: str | None,
        last_error: str | None,
        last_error_kind: str | None,
        clear_lease: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        identity = tuple(row[column] for column in _IDENTITY_COLUMNS)
        lease_owner = None if clear_lease else owner
        lease_expires = None if clear_lease else _lease_deadline(now)
        updated = conn.execute(
            """
            UPDATE connector_sync_checkpoints SET
                status=?,
                cursor=?,
                watermark=?,
                watermark_kind=?,
                upstream_total=?,
                pages_completed=?,
                records_received=?,
                records_applied=?,
                last_error=?,
                last_error_kind=?,
                lease_owner=?,
                lease_expires_at=?,
                row_version=row_version + 1,
                updated_at=?
            WHERE tenant_id=? AND connector_id=? AND store_id=?
              AND resource=? AND window_kind=? AND window_start=?
              AND window_end=?
              AND row_version=?
              AND (lease_owner=? OR lease_owner IS NULL)
            """,
            (
                status,
                cursor,
                watermark,
                watermark_kind,
                upstream_total,
                pages_completed,
                records_received,
                records_applied,
                last_error,
                last_error_kind,
                lease_owner,
                lease_expires,
                now,
                *identity,
                expected_version,
                owner,
            ),
        )
        if updated.rowcount != 1:
            raise SyncCheckpointConflict(
                "1688 availability sync checkpoint was updated concurrently"
            )
        return dict(self._get(conn, identity))

    @staticmethod
    def _get(conn: Any, identity: tuple[Any, ...]) -> Any:
        return conn.execute(
            """
            SELECT *
            FROM connector_sync_checkpoints
            WHERE tenant_id=? AND connector_id=? AND store_id=?
              AND resource=? AND window_kind=? AND window_start=?
              AND window_end=?
            """,
            identity,
        ).fetchone()


__all__ = [
    "AVAILABILITY_RESOURCE",
    "LEASE_SECONDS",
    "PAGE_RETRY_LIMIT",
    "STATUS_COMPLETE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "WATERMARK_EXHAUSTED",
    "WATERMARK_SOURCE_TIME",
    "WINDOW_FULL",
    "WINDOW_INCREMENTAL",
    "ConnectorSyncCheckpointService",
    "SyncCheckpointConflict",
    "availability_window",
    "public_checkpoint_view",
]
