"""M10-R WP1-03 / 门禁 #9 — 真实外生信号生产适配器（确定性）。

把 M7-R 已落库的实际报表/输入投影成 SignalGate 消费的候选信号序列，
并保持 tenant/store/SKU/date 隔离：

- 只取 ``listing_revisions`` 绑定到目标 store+SKU 的日级流量桶；
- 只使用 ``data_as_of >= metric_end`` 的行，避免陈旧 as-of 混入；
- 信号值 = 当日曝光 / max(此前曝光均值, 1)，只依赖过去数据，构造上无未来泄漏；
- 无字段证据时按行存在推断 actual；字段证据为 manual/demo 时对应降级，
  与 readiness 的 field evidence 权威源保持一致（D-035）。

本适配器只负责“生产消费入口”：没有真实信号时返回 None，由调用方按
missing/not_used 处理，禁止补零或伪造信号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import fmean
from typing import Any, Mapping

from ..readonly_data.contracts import EvidenceState, SourceKind


@dataclass(frozen=True)
class SignalInput:
    """一组按日期对齐的候选信号与可见性窗口。"""

    signal_by_date: Mapping[date, float]
    source_kind: SourceKind
    data_as_of: date | None
    source_reference: str | None = None
    signal_as_of: Mapping[date, date] = field(default_factory=dict)


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


def _as_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class TrafficSignalAdapter:
    """M7-R 流量事实 → 每日候选信号（租户/店铺/SKU/日期隔离）。"""

    def __init__(self, db: Any) -> None:
        self.db = db

    def _field_evidence_state(
        self, *, tenant_id: str, store_id: str
    ) -> EvidenceState | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT evidence_state
                FROM readonly_field_evidence
                WHERE tenant_id=? AND store_id=?
                  AND field_key='readiness:traffic_metric_buckets'
                ORDER BY data_as_of DESC, rowid DESC LIMIT 1
                """,
                (tenant_id, store_id),
            ).fetchone()
        if row is None:
            return None
        try:
            return EvidenceState(str(row["evidence_state"]))
        except ValueError:
            return None

    def load(
        self, *, tenant_id: str, store_id: str, sku_id: str
    ) -> SignalInput | None:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.metric_start, b.metric_end, b.data_as_of, b.impressions
                FROM traffic_metric_buckets b
                JOIN listing_revisions r
                  ON r.tenant_id = b.tenant_id AND r.id = b.listing_revision_id
                WHERE b.tenant_id=? AND r.store_id=? AND r.sku_id=?
                  AND b.bucket_granularity='day'
                ORDER BY b.metric_start ASC, b.data_as_of DESC, b.impressions DESC
                """,
                (tenant_id, store_id, sku_id),
            ).fetchall()
        if not rows:
            return None

        best_by_day: dict[date, tuple[date, int, date | None]] = {}
        latest_as_of: date | None = None
        for row in rows:
            start = _as_date(str(row["metric_start"]))
            end = _as_datetime(str(row["metric_end"]))
            as_of_dt = _as_datetime(str(row["data_as_of"]))
            if start is None or end is None:
                continue
            if as_of_dt is not None and as_of_dt < end:
                # 陈旧 as-of：该行在报告时点之前不可见，丢弃，防止伪造可见性。
                continue
            current = best_by_day.get(start)
            if current is None:
                as_of = as_of_dt.date() if as_of_dt is not None else None
                best_by_day[start] = (start, int(row["impressions"]), as_of)
            else:
                as_of = current[2]
            if latest_as_of is None or (as_of is not None and as_of > latest_as_of):
                latest_as_of = as_of

        if not best_by_day:
            return None

        ordered_days = sorted(best_by_day)
        signal_by_date: dict[date, float] = {}
        signal_as_of: dict[date, date] = {}
        previous_impressions: list[int] = []
        for day in ordered_days:
            impressions = best_by_day[day][1]
            day_as_of = best_by_day[day][2]
            if previous_impressions:
                mean_previous = fmean(previous_impressions)
                value = round(impressions / max(mean_previous, 1.0), 6)
            else:
                value = 1.0
            signal_by_date[day] = value
            if day_as_of is not None:
                signal_as_of[day] = day_as_of
            previous_impressions.append(impressions)

        state = self._field_evidence_state(tenant_id=tenant_id, store_id=store_id)
        if state is EvidenceState.MANUAL:
            source_kind = SourceKind.MANUAL
        elif state is EvidenceState.DEMO:
            source_kind = SourceKind.DEMO
        else:
            source_kind = SourceKind.ACTUAL
        return SignalInput(
            signal_by_date=signal_by_date,
            source_kind=source_kind,
            data_as_of=latest_as_of,
            source_reference=f"traffic_metric_buckets/{tenant_id}/{store_id}/{sku_id}",
            signal_as_of=signal_as_of,
        )
