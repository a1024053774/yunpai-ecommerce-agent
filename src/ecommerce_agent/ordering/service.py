from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

from .gate import OrderDraftGate
from .models import (
    EXTERNAL_STATE_REQUIRES_SOURCE,
    LEGAL_TRANSITIONS,
    OrderConfirmRequest,
    OrderDraftCreate,
    OrderDraftMode,
    OrderDraftView,
    OrderEventView,
    OrderStatusAdvanceRequest,
    PurchaseOrderStatus,
    legal_transition,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_rows(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class OrderingError(ValueError):
    pass


class OrderingService:
    """订购单草稿、人工确认与交付跟踪（V1 零外部写：不发送、不采购、不付款、不改库存）。"""

    def __init__(self, db: Any, *, gate: OrderDraftGate | None = None) -> None:
        self.db = db
        self.gate = gate or OrderDraftGate(db)

    # ---------- 行/视图转换 ----------

    @staticmethod
    def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(row)

    def _view(self, row: Mapping[str, Any]) -> OrderDraftView:
        data = self._row_to_dict(row)
        mode = OrderDraftMode(data["mode"])
        unsent_label = (
            "未发送（演示参数）" if mode is OrderDraftMode.DEMO else "未发送"
        )
        events = self._list_events(data["tenant_id"], data["order_draft_id"])
        return OrderDraftView(
            order_draft_id=data["order_draft_id"],
            tenant_id=data["tenant_id"],
            store_id=data["store_id"],
            sku_id=data["sku_id"],
            material_no=data["material_no"],
            supplier_ref=data["supplier_ref"],
            recommended_qty=int(data["recommended_qty"]),
            confirmed_qty=(
                int(data["confirmed_qty"]) if data["confirmed_qty"] is not None else None
            ),
            unit_cost=data["unit_cost"],
            currency=data["currency"],
            promised_delivery_at=data["promised_delivery_at"],
            forecast_run_ref=data["forecast_run_ref"],
            inventory_snapshot_ref=data["inventory_snapshot_ref"],
            policy_ref=data["policy_ref"],
            source_summary=data["source_summary"],
            assumptions=json.loads(data["assumptions_json"]),
            missing_fields=json.loads(data["missing_fields_json"]),
            mode=mode,
            status=PurchaseOrderStatus(data["status"]),
            version=int(data["version"]),
            created_by=data["created_by"],
            confirmed_by=data["confirmed_by"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            unsent_label=unsent_label,
            events=events,
        )

    # ---------- 事件 ----------

    def _list_events(self, tenant_id: str, order_draft_id: str) -> list[OrderEventView]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, order_draft_id, from_status, to_status,
                       actor, source_ref, note, created_at
                FROM purchase_order_events
                WHERE tenant_id = ? AND order_draft_id = ?
                ORDER BY rowid ASC
                """,
                (tenant_id, order_draft_id),
            ).fetchall()
        return [
            OrderEventView(
                event_id=str(row["event_id"]),
                order_draft_id=str(row["order_draft_id"]),
                from_status=str(row["from_status"]),
                to_status=str(row["to_status"]),
                actor=str(row["actor"]),
                source_ref=row["source_ref"],
                note=row["note"],
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _record_event(
        self,
        conn: Any,
        tenant_id: str,
        order_draft_id: str,
        from_status: PurchaseOrderStatus,
        to_status: PurchaseOrderStatus,
        actor: str,
        *,
        source_ref: str | None = None,
        note: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO purchase_order_events (
                event_id, tenant_id, order_draft_id, from_status, to_status,
                actor, source_ref, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                tenant_id,
                order_draft_id,
                from_status.value,
                to_status.value,
                actor,
                source_ref,
                note,
                _utc_now(),
            ),
        )

    # ---------- 草稿生成 ----------

    def create_draft(
        self,
        tenant_id: str,
        store_id: str,
        actor: str,
        payload: OrderDraftCreate,
    ) -> OrderDraftView:
        gate = self.gate.evaluate(tenant_id, store_id, payload)
        if not gate.allowed:
            raise OrderingError(
                f"ordering_gate_blocked:{','.join(gate.missing_fields)}"
            )
        material_no = gate.material_no or ""
        order_draft_id = uuid.uuid4().hex
        now = _utc_now()
        missing_fields = (
            [] if payload.mode is OrderDraftMode.FORMAL else ["formal_data_placeholder"]
        )
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO purchase_order_drafts (
                    order_draft_id, tenant_id, store_id, sku_id, material_no,
                    supplier_ref, recommended_qty, confirmed_qty, unit_cost,
                    currency, promised_delivery_at, forecast_run_ref,
                    inventory_snapshot_ref, policy_ref, source_summary,
                    assumptions_json, missing_fields_json, mode, status,
                    version, created_by, confirmed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_draft_id,
                    tenant_id,
                    store_id,
                    payload.sku_id,
                    material_no,
                    payload.supplier_ref,
                    payload.recommended_qty,
                    None,
                    payload.unit_cost,
                    payload.currency,
                    payload.promised_delivery_at,
                    payload.forecast_run_ref,
                    payload.inventory_snapshot_ref,
                    payload.policy_ref,
                    payload.source_summary,
                    _json_rows(payload.assumptions),
                    _json_rows(missing_fields),
                    payload.mode.value,
                    PurchaseOrderStatus.DRAFT.value,
                    1,
                    actor,
                    None,
                    now,
                    now,
                ),
            )
            self._record_event(
                conn,
                tenant_id,
                order_draft_id,
                PurchaseOrderStatus.DRAFT,
                PurchaseOrderStatus.DRAFT,
                actor,
                note=gate.reason,
            )
        return self.get(tenant_id, store_id, order_draft_id)

    # ---------- 查询 ----------

    def get(self, tenant_id: str, store_id: str, order_draft_id: str) -> OrderDraftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
        if row is None:
            raise OrderingError("ordering_draft_not_found")
        return self._view(row)

    def list(
        self,
        tenant_id: str,
        store_id: str,
        *,
        status: PurchaseOrderStatus | None = None,
        limit: int = 100,
    ) -> list[OrderDraftView]:
        clauses = ["tenant_id=?", "store_id=?"]
        params: list[Any] = [tenant_id, store_id]
        if status is not None:
            clauses.append("status=?")
            params.append(PurchaseOrderStatus(status).value)
        params.append(min(max(limit, 1), 500))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM purchase_order_drafts
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._view(row) for row in rows]

    # ---------- 状态操作 ----------

    def _load_current(
        self, tenant_id: str, store_id: str, order_draft_id: str
    ) -> tuple[dict[str, Any], Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
        if row is None:
            raise OrderingError("ordering_draft_not_found")
        return self._row_to_dict(row), row

    def submit_for_confirmation(
        self, tenant_id: str, store_id: str, order_draft_id: str, actor: str
    ) -> OrderDraftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
            if row is None:
                raise OrderingError("ordering_draft_not_found")
            current = self._row_to_dict(row)
            from_status = PurchaseOrderStatus(current["status"])
            to_status = PurchaseOrderStatus.AWAITING_CONFIRMATION
            if not legal_transition(from_status, to_status):
                raise OrderingError("ordering_status_transition_invalid")
            updated = conn.execute(
                """
                UPDATE purchase_order_drafts
                SET status=?, updated_at=?
                WHERE tenant_id=? AND store_id=? AND order_draft_id=?
                  AND status=?
                """,
                (
                    to_status.value,
                    _utc_now(),
                    tenant_id,
                    store_id,
                    order_draft_id,
                    from_status.value,
                ),
            )
            if updated.rowcount != 1:
                raise OrderingError("ordering_status_conflict")
            self._record_event(
                conn,
                tenant_id,
                order_draft_id,
                from_status,
                to_status,
                actor,
            )
        return self.get(tenant_id, store_id, order_draft_id)

    def confirm(
        self,
        tenant_id: str,
        store_id: str,
        order_draft_id: str,
        actor: str,
        payload: OrderConfirmRequest,
    ) -> OrderDraftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
            if row is None:
                raise OrderingError("ordering_draft_not_found")
            current = self._row_to_dict(row)
            from_status = PurchaseOrderStatus(current["status"])
            to_status = PurchaseOrderStatus.CONFIRMED
            if not legal_transition(from_status, to_status):
                raise OrderingError("ordering_status_transition_invalid")
            if int(current["version"]) != payload.version:
                raise OrderingError("ordering_version_conflict")
            updated = conn.execute(
                """
                UPDATE purchase_order_drafts
                SET status=?, confirmed_qty=?, supplier_ref=?,
                    promised_delivery_at=?, confirmed_by=?, version=version+1,
                    updated_at=?
                WHERE tenant_id=? AND store_id=? AND order_draft_id=?
                  AND version=?
                """,
                (
                    to_status.value,
                    payload.confirmed_qty,
                    payload.supplier_ref or current["supplier_ref"],
                    payload.promised_delivery_at or current["promised_delivery_at"],
                    actor,
                    _utc_now(),
                    tenant_id,
                    store_id,
                    order_draft_id,
                    payload.version,
                ),
            )
            if updated.rowcount != 1:
                raise OrderingError("ordering_version_conflict")
            self._record_event(
                conn,
                tenant_id,
                order_draft_id,
                from_status,
                to_status,
                actor,
                note="confirmed_by_human",
            )
        return self.get(tenant_id, store_id, order_draft_id)

    def cancel(
        self, tenant_id: str, store_id: str, order_draft_id: str, actor: str
    ) -> OrderDraftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
            if row is None:
                raise OrderingError("ordering_draft_not_found")
            current = self._row_to_dict(row)
            from_status = PurchaseOrderStatus(current["status"])
            to_status = PurchaseOrderStatus.CANCELLED
            if not legal_transition(from_status, to_status):
                raise OrderingError("ordering_status_transition_invalid")
            updated = conn.execute(
                """
                UPDATE purchase_order_drafts
                SET status=?, updated_at=?
                WHERE tenant_id=? AND store_id=? AND order_draft_id=?
                  AND status=?
                """,
                (
                    to_status.value,
                    _utc_now(),
                    tenant_id,
                    store_id,
                    order_draft_id,
                    from_status.value,
                ),
            )
            if updated.rowcount != 1:
                raise OrderingError("ordering_status_conflict")
            self._record_event(
                conn,
                tenant_id,
                order_draft_id,
                from_status,
                to_status,
                actor,
            )
        return self.get(tenant_id, store_id, order_draft_id)

    def advance_status(
        self,
        tenant_id: str,
        store_id: str,
        order_draft_id: str,
        actor: str,
        payload: OrderStatusAdvanceRequest,
    ) -> OrderDraftView:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM purchase_order_drafts
                WHERE tenant_id = ? AND store_id = ? AND order_draft_id = ?
                """,
                (tenant_id, store_id, order_draft_id),
            ).fetchone()
            if row is None:
                raise OrderingError("ordering_draft_not_found")
            current = self._row_to_dict(row)
            from_status = PurchaseOrderStatus(current["status"])
            to_status = PurchaseOrderStatus(payload.to_status)
            if not legal_transition(from_status, to_status):
                raise OrderingError("ordering_status_transition_invalid")
            if int(current["version"]) != payload.version:
                raise OrderingError("ordering_version_conflict")
            if (
                to_status in EXTERNAL_STATE_REQUIRES_SOURCE
                and not payload.source_ref
            ):
                raise OrderingError("ordering_external_state_requires_source")
            updated = conn.execute(
                """
                UPDATE purchase_order_drafts
                SET status=?, version=version+1, updated_at=?
                WHERE tenant_id=? AND store_id=? AND order_draft_id=?
                  AND version=?
                """,
                (
                    to_status.value,
                    _utc_now(),
                    tenant_id,
                    store_id,
                    order_draft_id,
                    payload.version,
                ),
            )
            if updated.rowcount != 1:
                raise OrderingError("ordering_version_conflict")
            self._record_event(
                conn,
                tenant_id,
                order_draft_id,
                from_status,
                to_status,
                actor,
                source_ref=payload.source_ref,
                note=payload.note,
            )
        return self.get(tenant_id, store_id, order_draft_id)
