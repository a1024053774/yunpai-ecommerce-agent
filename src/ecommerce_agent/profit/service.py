from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .models import (
    CATEGORY_LAYER,
    ExpenseCategory,
    LayerProjection,
    LedgerEntryInput,
    ProfitLayer,
    ProfitPolicyInput,
    ProfitProjectionView,
    ProfitScope,
    ReconciliationIssue,
    ReconciliationView,
    content_digest,
    layer_required_categories,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ProfitError(ValueError):
    pass


class ProfitService:
    """canonical 费用 ledger + 签收确认收入 + 三层利润（D-035：单一 ledger 权威源）。"""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ---------- 政策 ----------

    def register_policy(self, tenant_id: str, payload: ProfitPolicyInput) -> dict[str, Any]:
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM profit_policies WHERE tenant_id=? AND policy_version=?",
                (tenant_id, payload.policy_version),
            ).fetchone()
            if existing is not None:
                raise ProfitError("profit_policy_version_conflict")
            required: dict[str, list[str]] = {}
            if payload.required_categories is not None:
                required = {
                    layer.value: [category.value for category in categories]
                    for layer, categories in payload.required_categories.items()
                }
            conn.execute(
                """
                INSERT INTO profit_policies (
                    policy_id, tenant_id, revenue_recognition_basis,
                    required_categories_json, policy_version, active_from, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    tenant_id,
                    payload.revenue_recognition_basis.value,
                    json.dumps(required, ensure_ascii=False, sort_keys=True),
                    payload.policy_version,
                    _utc_now(),
                    _utc_now(),
                ),
            )
        return {
            "tenant_id": tenant_id,
            "policy_version": payload.policy_version,
            "revenue_recognition_basis": payload.revenue_recognition_basis.value,
        }

    def _active_policy(self, tenant_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM profit_policies
                WHERE tenant_id=?
                ORDER BY rowid DESC LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise ProfitError("profit_policy_not_registered")
        return dict(row)

    # ---------- ledger ----------

    def record_entry(self, tenant_id: str, payload: LedgerEntryInput) -> dict[str, Any]:
        if payload.scope is ProfitScope.FORMAL and payload.category in {
            ExpenseCategory.SIGNED_REVENUE,
            ExpenseCategory.REFUND_OFFSET,
        }:
            self._require_signed_receipt(
                tenant_id, payload.store_id, payload.order_id
            )
        digest = content_digest(payload.model_dump())
        entry_id = uuid.uuid4().hex
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO profit_ledger_entries (
                        entry_id, tenant_id, store_id, period, category, scope,
                        amount, currency, source_kind, sku_id, order_id,
                        mapping_version, entry_key, payload_hash, source_reference,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        tenant_id,
                        payload.store_id,
                        payload.period,
                        payload.category.value,
                        payload.scope.value,
                        payload.amount,
                        payload.currency,
                        payload.source_kind,
                        payload.sku_id,
                        payload.order_id,
                        payload.mapping_version,
                        payload.entry_key,
                        digest,
                        payload.source_reference,
                        _utc_now(),
                    ),
                )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ProfitError("profit_ledger_entry_duplicate") from exc
            raise
        return {
            "entry_id": entry_id,
            "entry_key": payload.entry_key,
            "tenant_id": tenant_id,
            "store_id": payload.store_id,
            "period": payload.period,
            "category": payload.category.value,
            "scope": payload.scope.value,
            "amount": payload.amount,
        }

    def _require_signed_receipt(
        self, tenant_id: str, store_id: str, order_id: str | None
    ) -> None:
        if not order_id:
            raise ProfitError("signed_receipt_requires_order")
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM commerce_orders
                WHERE tenant_id=? AND store_id=? AND external_order_id=?
                  AND order_status='delivered'
                """,
                (tenant_id, store_id, order_id),
            ).fetchone()
        if row is None:
            raise ProfitError("signed_receipt_required")

    def _entries(
        self,
        tenant_id: str,
        store_id: str,
        period: str,
        scope: ProfitScope,
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM profit_ledger_entries
                WHERE tenant_id=? AND store_id=? AND period=? AND scope=?
                """,
                (tenant_id, store_id, period, scope.value),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 投影 ----------

    def projection(
        self,
        tenant_id: str,
        store_id: str,
        period: str,
        scope: ProfitScope,
    ) -> ProfitProjectionView:
        policy = self._active_policy(tenant_id)
        try:
            policy_required: dict[str, list[str]] = json.loads(
                policy["required_categories_json"] or "{}"
            )
        except (TypeError, json.JSONDecodeError):
            policy_required = {}

        def required_for(layer: ProfitLayer) -> frozenset[ExpenseCategory]:
            custom = policy_required.get(layer.value)
            if custom:
                return frozenset(ExpenseCategory(item) for item in custom)
            return layer_required_categories(layer)

        entries = self._entries(tenant_id, store_id, period, scope)
        amounts: dict[ExpenseCategory, Decimal] = {}
        for entry in entries:
            category = ExpenseCategory(entry["category"])
            amounts[category] = amounts.get(category, Decimal("0")) + Decimal(
                entry["amount"]
            )
        present: set[ExpenseCategory] = set(amounts)
        demo_labels = scope is ProfitScope.DEMO

        def build_layer(
            layer: ProfitLayer,
            *,
            parent_available: bool = True,
            demo_label: str,
            formal_label: str,
        ) -> LayerProjection:
            if not parent_available:
                return LayerProjection(
                    layer=layer,
                    status="missing",
                    label=demo_label if demo_labels else formal_label,
                    missing_fields=[],
                )
            required = required_for(layer)
            missing = sorted(
                category.value for category in required if category not in present
            )
            if missing:
                return LayerProjection(
                    layer=layer,
                    status="missing",
                    label=demo_label if demo_labels else formal_label,
                    missing_fields=missing,
                )
            cumulative_layers = {
                ProfitLayer.SALES: frozenset({ProfitLayer.SALES}),
                ProfitLayer.OPERATING: frozenset(
                    {ProfitLayer.SALES, ProfitLayer.OPERATING}
                ),
                ProfitLayer.FINAL: frozenset(
                    {ProfitLayer.SALES, ProfitLayer.OPERATING, ProfitLayer.FINAL}
                ),
            }
            total = Decimal("0")
            for category, value in amounts.items():
                if CATEGORY_LAYER[category] in cumulative_layers[layer]:
                    total += value
            return LayerProjection(
                layer=layer,
                status="available",
                amount=str(total),
                label=demo_label if demo_labels else formal_label,
                missing_fields=[],
            )

        sales = build_layer(
            ProfitLayer.SALES,
            demo_label="销售利润试算（演示参数）",
            formal_label="销售利润",
        )
        operating = build_layer(
            ProfitLayer.OPERATING,
            parent_available=sales.status == "available",
            demo_label="经营利润试算（演示参数）",
            formal_label="经营利润",
        )
        final = build_layer(
            ProfitLayer.FINAL,
            parent_available=operating.status == "available",
            demo_label="净利润试算（演示参数）",
            formal_label="财务最终净利润",
        )
        if scope is ProfitScope.FORMAL:
            # 正式口径财务最终净利润仅授权视图展示：投影层通过 label 区分，
            # 服务层不对外输出隐藏能力；页面授权由路由层控制。
            pass
        return ProfitProjectionView(
            tenant_id=tenant_id,
            store_id=store_id,
            period=period,
            scope=scope,
            policy_version=policy["policy_version"],
            sales=sales,
            operating=operating,
            final=final,
            demo_labels=demo_labels,
        )

    # ---------- 对账 ----------

    def reconcile(
        self,
        tenant_id: str,
        store_id: str,
        period: str,
        scope: ProfitScope,
    ) -> ReconciliationView:
        entries = self._entries(tenant_id, store_id, period, scope)
        issues: list[ReconciliationIssue] = []
        seen_order_category: set[tuple[str, str]] = set()
        signed_orders: set[str] = set()
        ledger_identity: dict[tuple[str, str, str, str], set[str]] = {}
        for entry in entries:
            category = ExpenseCategory(entry["category"])
            identity = (
                category.value,
                entry["sku_id"] or "",
                entry["order_id"] or "",
                str(entry["amount"]),
            )
            ledger_identity.setdefault(identity, set()).add(entry["entry_key"])
            if entry["order_id"]:
                if category is ExpenseCategory.SIGNED_REVENUE:
                    signed_orders.add(entry["order_id"])
                key = (entry["order_id"], category.value)
                if key in seen_order_category:
                    issues.append(
                        ReconciliationIssue(
                            code="duplicate_order_category_entry",
                            entry_key=entry["entry_key"],
                            message=f"订单 {entry['order_id']} 的 {category.value} 重复入账",
                        )
                    )
                seen_order_category.add(key)
        for identity, entry_keys in ledger_identity.items():
            if len(entry_keys) > 1:
                issues.append(
                    ReconciliationIssue(
                        code="duplicate_ledger_entry",
                        entry_key=sorted(entry_keys)[0],
                        message=(
                            f"费用 {identity[0]} 重复入账 "
                            f"(store={store_id}, period={period}, amount={identity[3]})"
                        ),
                    )
                )
        for entry in entries:
            category = ExpenseCategory(entry["category"])
            if (
                category is ExpenseCategory.REFUND_OFFSET
                and entry["order_id"]
                and entry["order_id"] not in signed_orders
            ):
                # 退款可能跨期间滞后于签收收入：跨期间查找同店铺的签收收入，避免误报。
                with self.db.connect() as conn:
                    revenue = conn.execute(
                        """
                        SELECT 1 FROM profit_ledger_entries
                        WHERE tenant_id=? AND store_id=? AND scope=?
                          AND category='signed_receipt_revenue' AND order_id=?
                        LIMIT 1
                        """,
                        (tenant_id, store_id, scope.value, entry["order_id"]),
                    ).fetchone()
                if revenue is None:
                    issues.append(
                        ReconciliationIssue(
                            code="refund_without_signed_revenue",
                            entry_key=entry["entry_key"],
                            message=f"退款冲减 {entry['order_id']} 缺少对应签收确认收入",
                        )
                    )
        return ReconciliationView(
            tenant_id=tenant_id,
            store_id=store_id,
            period=period,
            scope=scope,
            entry_count=len(entries),
            issues=issues,
            double_count_ok=not issues,
        )
