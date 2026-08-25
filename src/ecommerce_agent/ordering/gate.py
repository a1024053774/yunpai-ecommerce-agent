from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import DraftGateResult, OrderDraftCreate, OrderDraftMode


FORMAL_GATE_FIELDS = (
    "material_no",
    "forecast_run_ref",
    "inventory_plan",
    "recommended_qty",
    "supply_constraint",
    "delivery_constraint",
)


class OrderDraftGate:
    """草稿生成 Gate：料号须绑定 SKU，补货证据须最新且绑定库存计划。"""

    def __init__(self, db: Any) -> None:
        self.db = db

    def resolve_material_no(
        self,
        tenant_id: str,
        store_id: str,
        sku_id: str,
        requested: str | None,
    ) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT c.internal_part_number
                FROM readonly_product_mapping_events m
                JOIN readonly_canonical_products c
                  ON c.tenant_id = m.tenant_id
                 AND c.store_id = m.store_id
                 AND c.canonical_product_id = m.canonical_product_id
                WHERE m.tenant_id = ? AND m.store_id = ?
                  AND m.sku_id = ? AND m.event_type = 'confirmed'
                ORDER BY m.mapping_version DESC
                LIMIT 1
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        resolved = str(row[0]) if row and row[0] else None
        if requested is not None:
            return requested if requested == resolved else None
        return resolved

    def _latest_completed_run(
        self, tenant_id: str, store_id: str, sku_id: str
    ) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT run_id FROM forecast_runs
                WHERE tenant_id=? AND store_id=? AND sku_id=? AND status='completed'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        return str(row[0]) if row else None

    def _forecast_evidence(
        self, tenant_id: str, store_id: str, sku_id: str, run_ref: str
    ) -> bool:
        return self._latest_completed_run(tenant_id, store_id, sku_id) == run_ref

    def _advisory_plan(
        self,
        tenant_id: str,
        store_id: str,
        sku_id: str,
        plan_ref: str | None,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            if plan_ref is not None:
                row = conn.execute(
                    """
                    SELECT * FROM inventory_plans
                    WHERE tenant_id=? AND store_id=? AND sku_id=? AND plan_id=?
                      AND quantity_status='advisory'
                    """,
                    (tenant_id, store_id, sku_id, plan_ref),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM inventory_plans
                    WHERE tenant_id=? AND store_id=? AND sku_id=?
                      AND quantity_status='advisory'
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (tenant_id, store_id, sku_id),
                ).fetchone()
        return dict(row) if row else None

    def _quantity_matches(self, plan: dict[str, Any], recommended_qty: int) -> bool:
        try:
            expected = Decimal(str(plan["recommended_order_qty"]))
            actual = Decimal(str(recommended_qty))
        except (InvalidOperation, TypeError, KeyError):
            return False
        return expected == actual

    def _supply_constraint_evidence(
        self, tenant_id: str, store_id: str, sku_id: str, policy_ref: str | None
    ) -> bool:
        # 供货约束只认 per-SKU 补货策略（已绑定 SKU/料号），并校验生效期与新鲜度；
        # 不再接受无 SKU 绑定的店铺级 field evidence 兜底（P1 收口）。
        # 时间统一解析为 aware UTC 再比较，避免混时区 ISO 字符串字典序误判。
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=90)
        with self.db.connect() as conn:
            if policy_ref is not None:
                rows = conn.execute(
                    """
                    SELECT active_from, created_at FROM inventory_planning_policies
                    WHERE tenant_id = ? AND store_id = ? AND sku_id = ?
                      AND policy_id = ? AND supplier_lead_days >= 0
                    """,
                    (tenant_id, store_id, sku_id, policy_ref),
                ).fetchall()
                for row in rows:
                    active = self._parse_dt(row["active_from"])
                    created = self._parse_dt(row["created_at"])
                    if (
                        active is not None and active <= now
                        and created is not None and cutoff <= created <= now
                    ):
                        return True
            rows = conn.execute(
                """
                SELECT active_from, created_at FROM inventory_planning_policies
                WHERE tenant_id = ? AND store_id = ?
                  AND sku_id = ? AND supplier_lead_days >= 0
                ORDER BY created_at DESC, rowid DESC LIMIT 5
                """,
                (tenant_id, store_id, sku_id),
            ).fetchall()
        for row in rows:
            active = self._parse_dt(row["active_from"])
            created = self._parse_dt(row["created_at"])
            if (
                active is not None and active <= now
                and created is not None and cutoff <= created <= now
            ):
                return True
        return False

    def _delivery_constraint_evidence(
        self, tenant_id: str, store_id: str
    ) -> bool:
        # 交期暂无 SKU 级数据源：保持店铺级 field evidence，但强制新鲜度
        # （data_as_of 不晚于 now 且 ≤30 天）与来源引用（P1 收口）。
        # 时间统一解析为 aware UTC 再比较，避免混时区 ISO 字符串字典序误判。
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=30)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT data_as_of, source_reference FROM readonly_field_evidence
                WHERE tenant_id = ? AND store_id = ?
                  AND field_key = 'readiness:transport_lead_days'
                  AND evidence_state IN ('actual', 'manual')
                """,
                (tenant_id, store_id),
            ).fetchall()
        for row in rows:
            if not row["source_reference"] or not str(row["source_reference"]).strip():
                continue
            data_as_of = self._parse_dt(row["data_as_of"])
            if data_as_of is not None and data_as_of <= now and data_as_of >= cutoff:
                return True
        return False

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        """把 ISO 字符串解析为 aware UTC；naive 按代码库约定视为 UTC。"""

        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def evaluate(
        self,
        tenant_id: str,
        store_id: str,
        payload: OrderDraftCreate,
    ) -> DraftGateResult:
        material_no = self.resolve_material_no(
            tenant_id, store_id, payload.sku_id, payload.material_no
        )
        if payload.mode is OrderDraftMode.DEMO:
            return DraftGateResult(
                allowed=True,
                mode=payload.mode,
                material_no=material_no,
                missing_fields=[],
                reason="demo_draft_allowed_with_labels",
            )

        plan = self._advisory_plan(
            tenant_id, store_id, payload.sku_id, payload.inventory_snapshot_ref
        )
        missing: list[str] = []
        if not material_no:
            missing.append("material_no")
        if not payload.forecast_run_ref or not self._forecast_evidence(
            tenant_id, store_id, payload.sku_id, payload.forecast_run_ref
        ):
            missing.append("forecast_run_ref")
        if plan is None:
            missing.append("inventory_plan")
        elif str(plan.get("forecast_run_id")) != payload.forecast_run_ref:
            missing.append("inventory_plan")
        else:
            if not self._quantity_matches(plan, payload.recommended_qty):
                missing.append("recommended_qty")
        if not self._supply_constraint_evidence(
            tenant_id, store_id, payload.sku_id, payload.policy_ref
        ):
            missing.append("supply_constraint")
        if not self._delivery_constraint_evidence(tenant_id, store_id):
            missing.append("delivery_constraint")

        if missing:
            return DraftGateResult(
                allowed=False,
                mode=payload.mode,
                material_no=material_no,
                missing_fields=missing,
                reason="formal_draft_gate_blocked",
            )
        return DraftGateResult(
            allowed=True,
            mode=payload.mode,
            material_no=material_no,
            missing_fields=[],
            reason="formal_draft_gate_passed",
        )
