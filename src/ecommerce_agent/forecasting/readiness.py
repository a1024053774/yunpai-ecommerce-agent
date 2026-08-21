"""M10-R WP1 — 预测/补货信号适配与数据准备度投影。

把预测/补货输入统一投影为五类准备度（预测目标/候选信号/供给约束/
交付约束/执行主数据），每项输入记录证据四态、来源类型、data_as_of、
时间粒度、SKU/料号覆盖和缺失原因。

证据解析顺序：
1. M7-R WP1 ``readonly_data.ReadonlyDataService.list_field_evidence`` 的
   ``readiness:<input_key>`` field evidence 为权威来源；
2. 无 field evidence 时回退到按行存在推断，并在 ``missing_reason``
   注明“未登记 field evidence，按行存在推断”。

竞品信号遵循 D-025：只统计 ``competitive_entity_matches`` 中
``status='approved'`` 的 subject_sku；原始观测行不作为信号依据。
候选信号是否进入 champion 的 rolling backtest 门禁归 WP2，本轮不实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..readonly_data.contracts import EvidenceState, SourceKind
from ..readonly_data.service import ReadonlyDataService


FIELD_KEY_PREFIX = "readiness:"


class ReadinessCategory(StrEnum):
    FORECAST_TARGET = "forecast_target"
    CANDIDATE_SIGNAL = "candidate_signal"
    SUPPLY_CONSTRAINT = "supply_constraint"
    DELIVERY_CONSTRAINT = "delivery_constraint"
    MASTER_DATA = "master_data"


class TimeGranularity(StrEnum):
    DAILY = "daily"
    HOURLY = "hourly"
    MONTHLY = "monthly"


def _field_key(input_key: str) -> str:
    return f"{FIELD_KEY_PREFIX}{input_key}"


def _source_kind_for(state: EvidenceState) -> SourceKind | None:
    if state is EvidenceState.ACTUAL:
        return SourceKind.ACTUAL
    if state is EvidenceState.MANUAL:
        return SourceKind.MANUAL
    if state is EvidenceState.DEMO:
        return SourceKind.DEMO
    return None


@dataclass(frozen=True)
class ReadinessInput:
    """单个预测/补货输入的准备度条目。"""

    input_key: str
    field_key: str
    category: ReadinessCategory
    label: str
    evidence_state: EvidenceState
    source_kind: SourceKind | None
    data_as_of: str | None
    granularity: TimeGranularity | None
    sku_coverage: int | None
    material_coverage: int | None
    missing_reason: str | None
    source_reference: str | None = None


def _parse_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return value


class SignalReadinessService:
    """把现有事实表投影为五类准备度。"""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._readonly = ReadonlyDataService(db)

    def _stats(
        self,
        *,
        tenant_id: str,
        store_id: str,
        table: str,
        time_column: str,
        sku_expression: str | None,
        store_scoped: bool = True,
    ) -> tuple[int, str | None, int | None]:
        where = "tenant_id=?"
        params: list[Any] = [tenant_id]
        if store_scoped:
            where += " AND store_id=?"
            params.append(store_id)
        with self._db.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS cnt,
                           MAX({time_column}) AS latest,
                           {sku_expression or "NULL"} AS sku_count
                    FROM {table}
                    WHERE {where}""",
                params,
            ).fetchone()
        return (
            int(row["cnt"]),
            _parse_time(row["latest"]),
            int(row["sku_count"]) if row["sku_count"] is not None else None,
        )

    def _competitor_approved_stats(
        self, *, tenant_id: str, store_id: str
    ) -> tuple[int, str | None, int | None]:
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT subject_sku) AS cnt,
                          MAX(observed_at) AS latest
                   FROM competitive_entity_matches
                   WHERE tenant_id=? AND store_id=? AND status='approved'""",
                (tenant_id, store_id),
            ).fetchone()
        return (
            int(row["cnt"]),
            _parse_time(row["latest"]),
            int(row["cnt"]),
        )

    def _traffic_sku_coverage(
        self, *, tenant_id: str, store_id: str
    ) -> int | None:
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT r.sku_id) AS sku_count
                   FROM traffic_metric_buckets b
                   JOIN listing_revisions r ON r.id = b.listing_revision_id
                   WHERE b.tenant_id=? AND r.store_id=?""",
                (tenant_id, store_id),
            ).fetchone()
        return int(row["sku_count"]) if row["sku_count"] is not None else None

    def _traffic_stats(
        self, *, tenant_id: str, store_id: str
    ) -> tuple[int, str | None, int | None]:
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS cnt,
                          MAX(b.data_as_of) AS latest,
                          COUNT(DISTINCT r.sku_id) AS sku_count
                   FROM traffic_metric_buckets b
                   JOIN listing_revisions r ON r.id = b.listing_revision_id
                   WHERE b.tenant_id=? AND r.store_id=?""",
                (tenant_id, store_id),
            ).fetchone()
        return (
            int(row["cnt"]),
            _parse_time(row["latest"]),
            int(row["sku_count"]) if row["sku_count"] is not None else None,
        )

    def _material_mapping_stats(
        self, *, tenant_id: str, store_id: str
    ) -> tuple[int, str | None, int | None]:
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT internal_part_number) AS cnt,
                          MAX(created_at) AS latest
                   FROM readonly_canonical_products
                   WHERE tenant_id=? AND store_id=?""",
                (tenant_id, store_id),
            ).fetchone()
        return (
            int(row["cnt"]),
            _parse_time(row["latest"]),
            int(row["cnt"]),
        )

    def _after_sale_stats(
        self, *, tenant_id: str, store_id: str
    ) -> tuple[int, str | None]:
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS cnt, MAX(a.opened_at) AS latest
                   FROM commerce_after_sale_cases a
                   JOIN commerce_orders o ON o.id = a.order_id
                   WHERE o.tenant_id=? AND o.store_id=?""",
                (tenant_id, store_id),
            ).fetchone()
        return int(row["cnt"]), _parse_time(row["latest"])

    def _inferred_inputs(
        self, *, tenant_id: str, store_id: str
    ) -> list[ReadinessInput]:
        items: list[ReadinessInput] = []

        def append(
            *,
            input_key: str,
            category: ReadinessCategory,
            label: str,
            state: EvidenceState,
            data_as_of: str | None,
            sku_coverage: int | None,
            granularity: TimeGranularity | None,
            missing_reason: str,
            source_kind: SourceKind | None = None,
            material_coverage: int | None = None,
        ) -> None:
            items.append(
                ReadinessInput(
                    input_key=input_key,
                    field_key=_field_key(input_key),
                    category=category,
                    label=label,
                    evidence_state=state,
                    source_kind=source_kind,
                    data_as_of=data_as_of,
                    granularity=granularity,
                    sku_coverage=sku_coverage,
                    material_coverage=material_coverage,
                    missing_reason=missing_reason,
                )
            )

        # 预测目标
        count, latest, skus = self._stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="demand_daily_facts",
            time_column="business_date",
            sku_expression="COUNT(DISTINCT sku_id)",
        )
        append(
            input_key="demand_daily_facts",
            category=ReadinessCategory.FORECAST_TARGET,
            label="每日需求事实（store+SKU）",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=TimeGranularity.DAILY,
            missing_reason=None
            if count > 0
            else "无需求事实：等待订单/需求导入（M7-R WP1 导入契约）",
        )

        # 候选信号：流量（按店铺隔离）
        count, latest, traffic_skus = self._traffic_stats(
            tenant_id=tenant_id, store_id=store_id
        )
        append(
            input_key="traffic_metric_buckets",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="流量曝光/点击（revision→SKU）",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=traffic_skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无流量数据：M9-R/Traffic Lab 接入后可用",
        )

        # 候选信号：广告
        count, latest, _ = self._stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="marketing_campaign_metrics",
            time_column="metric_date",
            sku_expression=None,
        )
        append(
            input_key="marketing_campaign_metrics",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="广告投放指标",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=None,
            granularity=TimeGranularity.DAILY,
            missing_reason=None
            if count > 0
            else "无广告数据：等待营销指标导入",
        )

        # 候选信号：竞品（approved-only，D-025）
        count, latest, skus = self._competitor_approved_stats(
            tenant_id=tenant_id, store_id=store_id
        )
        append(
            input_key="competitor_approved_signal",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="竞品信号（approved-only）",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无已批准竞品匹配（D-025 approved-only）",
        )

        # 候选信号：退款/售后
        count, latest = self._after_sale_stats(
            tenant_id=tenant_id, store_id=store_id
        )
        append(
            input_key="after_sale_cases",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="退款/售后",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=None,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无退款/售后记录：等待订单售后导入（SKU 经订单行关联）",
        )

        # 供给约束
        count, latest, skus = self._stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="inventory_balances",
            time_column="source_updated_at",
            sku_expression="COUNT(DISTINCT sku_id)",
        )
        append(
            input_key="inventory_balances",
            category=ReadinessCategory.SUPPLY_CONSTRAINT,
            label="可售/在途/预留库存",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无库存快照：等待库存导入",
        )

        count, latest, skus = self._stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="inventory_planning_policies",
            time_column="active_from",
            sku_expression="COUNT(DISTINCT sku_id)",
        )
        append(
            input_key="inventory_planning_policies",
            category=ReadinessCategory.SUPPLY_CONSTRAINT,
            label="补货策略（lead/review/MOQ/服务水平）",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无补货策略：等待策略配置",
        )

        # 交付约束：供应商周期（策略为人工配置）
        append(
            input_key="supplier_lead_days",
            category=ReadinessCategory.DELIVERY_CONSTRAINT,
            label="供应商生产/备货周期",
            state=EvidenceState.MANUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无补货策略/供应商参数",
            source_kind=SourceKind.MANUAL if count > 0 else None,
        )

        append(
            input_key="transport_lead_days",
            category=ReadinessCategory.DELIVERY_CONSTRAINT,
            label="运输周期",
            state=EvidenceState.MISSING,
            data_as_of=None,
            sku_coverage=None,
            granularity=None,
            missing_reason="运输周期未接入（M7-R 财务/物流输入）",
        )

        # 执行主数据
        count, latest, skus = self._stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="catalog_items",
            time_column="source_updated_at",
            sku_expression="COUNT(DISTINCT sku_id)",
        )
        append(
            input_key="catalog_items",
            category=ReadinessCategory.MASTER_DATA,
            label="商品/SKU",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason=None
            if count > 0
            else "无商品主数据：等待 catalog 导入",
        )

        count, latest, materials = self._material_mapping_stats(
            tenant_id=tenant_id, store_id=store_id
        )
        append(
            input_key="material_no_mapping",
            category=ReadinessCategory.MASTER_DATA,
            label="内部料号映射",
            state=EvidenceState.ACTUAL if count > 0 else EvidenceState.MISSING,
            data_as_of=latest,
            sku_coverage=None,
            material_coverage=materials,
            granularity=None,
            missing_reason=None
            if count > 0
            else "料号映射未完成（M7-R WP3 canonical 产品未登记）",
        )

        return items

    def project(
        self, *, tenant_id: str, store_id: str
    ) -> list[ReadinessInput]:
        evidence: dict[str, dict[str, Any]] = {
            item["field_key"]: item
            for item in self._readonly.list_field_evidence(
                tenant_id, store_id=store_id
            )
        }
        resolved: list[ReadinessInput] = []
        for item in self._inferred_inputs(
            tenant_id=tenant_id, store_id=store_id
        ):
            record = evidence.get(item.field_key)
            if record is None:
                missing_reason = item.missing_reason
                if item.evidence_state is EvidenceState.MISSING and missing_reason:
                    missing_reason = f"{missing_reason}（未登记 field evidence，按行存在推断）"
                resolved.append(
                    ReadinessInput(
                        input_key=item.input_key,
                        field_key=item.field_key,
                        category=item.category,
                        label=item.label,
                        evidence_state=item.evidence_state,
                        source_kind=item.source_kind,
                        data_as_of=item.data_as_of,
                        granularity=item.granularity,
                        sku_coverage=item.sku_coverage,
                        material_coverage=item.material_coverage,
                        missing_reason=missing_reason,
                        source_reference=item.source_reference,
                    )
                )
                continue

            state = EvidenceState(record["evidence_state"])
            resolved.append(
                ReadinessInput(
                    input_key=item.input_key,
                    field_key=item.field_key,
                    category=item.category,
                    label=item.label,
                    evidence_state=state,
                    source_kind=_source_kind_for(state),
                    data_as_of=record.get("data_as_of") or item.data_as_of,
                    granularity=item.granularity,
                    sku_coverage=item.sku_coverage,
                    material_coverage=item.material_coverage,
                    missing_reason=(
                        record.get("reason") if state is EvidenceState.MISSING else None
                    ),
                    source_reference=record.get("source_reference"),
                )
            )
        return resolved

    def summary(
        self, *, tenant_id: str, store_id: str
    ) -> dict[str, dict[str, int]]:
        """按类别 × 证据状态汇总准备度计数。"""
        result: dict[str, dict[str, int]] = {}
        for item in self.project(tenant_id=tenant_id, store_id=store_id):
            category = result.setdefault(item.category.value, {})
            category[item.evidence_state.value] = (
                category.get(item.evidence_state.value, 0) + 1
            )
        return result
