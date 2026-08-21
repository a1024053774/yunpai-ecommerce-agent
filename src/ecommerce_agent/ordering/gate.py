from __future__ import annotations

from typing import Any, Mapping

from .models import DraftGateResult, OrderDraftCreate, OrderDraftMode


FORMAL_GATE_FIELDS = (
    "material_no",
    "forecast_run_ref",
    "supply_constraint",
    "delivery_constraint",
)


class OrderDraftGate:
    """草稿生成 Gate：canonical 料号 + 补货证据 + 必需供货约束（D-035 单一实现）。"""

    def __init__(self, db: Any) -> None:
        self.db = db

    def resolve_material_no(
        self,
        tenant_id: str,
        store_id: str,
        sku_id: str,
        requested: str | None,
    ) -> str | None:
        if requested is not None:
            with self.db.connect() as conn:
                exists = conn.execute(
                    """
                    SELECT 1 FROM readonly_canonical_products
                    WHERE tenant_id=? AND store_id=? AND internal_part_number=?
                    """,
                    (tenant_id, store_id, requested),
                ).fetchone()
            return requested if exists is not None else None
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
        return str(row[0]) if row and row[0] else None

    def _forecast_evidence(
        self, tenant_id: str, store_id: str, sku_id: str, run_ref: str
    ) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM forecast_runs
                WHERE tenant_id = ? AND store_id = ? AND run_id = ?
                  AND sku_id = ? AND status = 'completed'
                """,
                (tenant_id, store_id, run_ref, sku_id),
            ).fetchone()
        return row is not None

    def _supply_constraint_evidence(
        self, tenant_id: str, store_id: str, sku_id: str, policy_ref: str | None
    ) -> bool:
        with self.db.connect() as conn:
            if policy_ref is not None:
                row = conn.execute(
                    """
                    SELECT 1 FROM inventory_planning_policies
                    WHERE tenant_id = ? AND store_id = ? AND sku_id = ?
                      AND policy_id = ? AND supplier_lead_days >= 0
                    """,
                    (tenant_id, store_id, sku_id, policy_ref),
                ).fetchone()
                if row is not None:
                    return True
            evidence = conn.execute(
                """
                SELECT 1 FROM readonly_field_evidence
                WHERE tenant_id = ? AND store_id = ?
                  AND field_key = 'readiness:supplier_lead_days'
                  AND evidence_state IN ('actual', 'manual')
                LIMIT 1
                """,
                (tenant_id, store_id),
            ).fetchone()
        return evidence is not None

    def _delivery_constraint_evidence(
        self, tenant_id: str, store_id: str
    ) -> bool:
        with self.db.connect() as conn:
            evidence = conn.execute(
                """
                SELECT 1 FROM readonly_field_evidence
                WHERE tenant_id = ? AND store_id = ?
                  AND field_key = 'readiness:transport_lead_days'
                  AND evidence_state IN ('actual', 'manual')
                LIMIT 1
                """,
                (tenant_id, store_id),
            ).fetchone()
        return evidence is not None

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
        missing: list[str] = []
        if not material_no:
            missing.append("material_no")
        if not payload.forecast_run_ref or not self._forecast_evidence(
            tenant_id, store_id, payload.sku_id, payload.forecast_run_ref
        ):
            missing.append("forecast_run_ref")
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
