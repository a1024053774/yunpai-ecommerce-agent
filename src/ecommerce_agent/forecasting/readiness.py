"""M10-R WP1 — 预测/补货信号适配与数据准备度投影（骨架）。

把预测/补货输入统一投影为五类准备度，每项输入记录证据四态
（actual/manual/demo/missing）、来源类型、data_as_of、时间粒度、
SKU/料号覆盖和缺失原因。证据四态复用 M7-R WP1 的
``readonly_data.contracts.EvidenceState``。

当前为骨架实现：证据状态按表内行存在性推断；后续接 M7-R
``ReadonlyDataService`` 的 field evidence 作为权威证据源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..readonly_data.contracts import EvidenceState, SourceKind


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


@dataclass(frozen=True)
class ReadinessInput:
    """单个预测/补货输入的准备度条目。"""

    input_key: str
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
    """把现有事实表投影为五类准备度（骨架）。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _table_stats(
        self,
        *,
        tenant_id: str,
        store_id: str,
        table: str,
        time_column: str,
        sku_column: str | None,
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
                           {sku_column or "NULL"} AS sku_count
                    FROM {table}
                    WHERE {where}""",
                params,
            ).fetchone()
        return (
            int(row["cnt"]),
            _parse_time(row["latest"]),
            int(row["sku_count"]) if row["sku_count"] is not None else None,
        )

    def project(
        self, *, tenant_id: str, store_id: str
    ) -> list[ReadinessInput]:
        inputs: list[ReadinessInput] = []

        def add(
            *,
            input_key: str,
            category: ReadinessCategory,
            label: str,
            count: int,
            data_as_of: str | None,
            sku_coverage: int | None,
            granularity: TimeGranularity | None,
            missing_reason: str,
        ) -> None:
            if count > 0:
                state = EvidenceState.ACTUAL
            else:
                state = EvidenceState.MISSING
            inputs.append(
                ReadinessInput(
                    input_key=input_key,
                    category=category,
                    label=label,
                    evidence_state=state,
                    source_kind=None,
                    data_as_of=data_as_of,
                    granularity=granularity,
                    sku_coverage=sku_coverage,
                    material_coverage=None,
                    missing_reason=None if count > 0 else missing_reason,
                )
            )

        # 预测目标
        count, latest, skus = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="demand_daily_facts",
            time_column="business_date",
            sku_column="COUNT(DISTINCT sku_id)",
        )
        add(
            input_key="demand_daily_facts",
            category=ReadinessCategory.FORECAST_TARGET,
            label="每日需求事实（store+SKU）",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=TimeGranularity.DAILY,
            missing_reason="无需求事实：等待订单/需求导入（M7-R WP1 导入契约）",
        )

        # 候选信号
        count, latest, _ = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="traffic_metric_buckets",
            time_column="data_as_of",
            sku_column=None,
            store_scoped=False,
        )
        add(
            input_key="traffic_metric_buckets",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="流量曝光/点击（revision 级）",
            count=count,
            data_as_of=latest,
            sku_coverage=None,
            granularity=None,
            missing_reason="无流量数据：M9-R/Traffic Lab 接入后可用",
        )

        count, latest, _ = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="marketing_campaign_metrics",
            time_column="metric_date",
            sku_column=None,
        )
        add(
            input_key="marketing_campaign_metrics",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="广告投放指标",
            count=count,
            data_as_of=latest,
            sku_coverage=None,
            granularity=TimeGranularity.DAILY,
            missing_reason="无广告数据：等待营销指标导入",
        )

        count, latest, skus = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="competitor_observations",
            time_column="observed_at",
            sku_column="COUNT(DISTINCT subject_sku)",
        )
        add(
            input_key="competitor_observations",
            category=ReadinessCategory.CANDIDATE_SIGNAL,
            label="竞品观测（approved-only 待接线）",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason="无竞品观测：等待 F-304 approved-only 接线",
        )

        # 供给约束
        count, latest, skus = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="inventory_balances",
            time_column="source_updated_at",
            sku_column="COUNT(DISTINCT sku_id)",
        )
        add(
            input_key="inventory_balances",
            category=ReadinessCategory.SUPPLY_CONSTRAINT,
            label="可售/在途/预留库存",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason="无库存快照：等待库存导入",
        )

        count, latest, skus = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="inventory_planning_policies",
            time_column="active_from",
            sku_column="COUNT(DISTINCT sku_id)",
        )
        add(
            input_key="inventory_planning_policies",
            category=ReadinessCategory.SUPPLY_CONSTRAINT,
            label="补货策略（lead/review/MOQ/服务水平）",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason="无补货策略：等待策略配置",
        )

        # 交付约束
        add(
            input_key="supplier_lead_days",
            category=ReadinessCategory.DELIVERY_CONSTRAINT,
            label="供应商生产/备货周期",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason="无补货策略/供应商参数",
        )
        add(
            input_key="transport_lead_days",
            category=ReadinessCategory.DELIVERY_CONSTRAINT,
            label="运输周期",
            count=0,
            data_as_of=None,
            sku_coverage=None,
            granularity=None,
            missing_reason="运输周期未接入（M7-R 财务/物流输入）",
        )

        # 执行主数据
        count, latest, skus = self._table_stats(
            tenant_id=tenant_id,
            store_id=store_id,
            table="catalog_items",
            time_column="source_updated_at",
            sku_column="COUNT(DISTINCT sku_id)",
        )
        add(
            input_key="catalog_items",
            category=ReadinessCategory.MASTER_DATA,
            label="商品/SKU",
            count=count,
            data_as_of=latest,
            sku_coverage=skus,
            granularity=None,
            missing_reason="无商品主数据：等待 catalog 导入",
        )
        add(
            input_key="material_no_mapping",
            category=ReadinessCategory.MASTER_DATA,
            label="内部料号映射",
            count=0,
            data_as_of=None,
            sku_coverage=None,
            granularity=None,
            missing_reason="料号映射未完成（M7-R WP3）",
        )

        return inputs

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
