from __future__ import annotations

from datetime import UTC, date, datetime

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting import ForecastRunService
from ecommerce_agent.forecasting.signal_adapter import SignalInput, TrafficSignalAdapter
from ecommerce_agent.readonly_data import (
    EvidenceState,
    FieldEvidenceInput,
    ImportManifestInput,
    ImportReference,
    ReadonlyDataService,
    SourceKind,
    content_digest,
    schema_fingerprint,
)


TENANT = "tenant-signal"
STORE = "store-signal"
SKU = "sku-signal"


def _make_db(path) -> Database:
    db = Database(path)
    db.initialize()
    return db


def _seed_revision_and_buckets(
    conn,
    *,
    store_id: str = STORE,
    sku_id: str = SKU,
    days: list[tuple[str, int, str]] | None = None,
) -> None:
    asset_digest = (f"asset-{store_id}-{sku_id}" + "f" * 80)[:64]
    conn.execute(
        """INSERT INTO creative_assets (
            asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
            feature_schema_version, payload_hash, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"asset-{store_id}-{sku_id}",
            TENANT,
            asset_digest,
            "image/png",
            100,
            100,
            "objects/creative/x.png",
            "image-v1",
            asset_digest,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    revision = f"rev-{store_id}-{sku_id}"
    conn.execute(
        """INSERT INTO listing_revisions (
            id, tenant_id, connector_id, store_id, item_id, sku_id,
            revision_no, title, main_image_asset_id, sale_price,
            attributes_json, active_from, source_updated_at, payload_hash,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            revision,
            TENANT,
            "connector-1",
            store_id,
            f"item-{sku_id}",
            sku_id,
            1,
            "标题",
            f"asset-{store_id}-{sku_id}",
            "10.00",
            "{}",
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
            "c" * 64,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    for offset, (day, impressions, as_of) in enumerate(days or []):
        conn.execute(
            """INSERT INTO traffic_metric_buckets (
                id, tenant_id, connector_id, listing_revision_id,
                metric_start, metric_end, bucket_granularity, traffic_source,
                impressions, clicks, visitors, favorites, cart_adds, orders,
                sales_amount, ad_spend, search_impressions, recommend_impressions,
                data_as_of, source_id, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"bucket-{store_id}-{sku_id}-{offset}",
                TENANT,
                "connector-1",
                revision,
                f"{day}T00:00:00+00:00",
                f"{day}T23:59:59+00:00",
                "day",
                "search",
                impressions,
                0,
                0,
                0,
                0,
                0,
                "0.00",
                "0.00",
                0,
                0,
                as_of,
                f"src-{store_id}-{sku_id}-{offset}",
                "d" * 64,
                1,
                as_of,
                as_of,
            ),
        )


def _signal(tmp_path, *, store_id=STORE, sku_id=SKU, days=None) -> TrafficSignalAdapter:
    db = _make_db(tmp_path / "signal.sqlite3")
    if days is not None:
        with db.connect() as conn:
            _seed_revision_and_buckets(conn, store_id=store_id, sku_id=sku_id, days=days)
    return TrafficSignalAdapter(db)


def _insert_bucket(
    conn,
    *,
    bucket_id: str,
    revision: str,
    day: str,
    impressions: int,
    as_of: str,
    source_id: str,
) -> None:
    conn.execute(
        """INSERT INTO traffic_metric_buckets (
            id, tenant_id, connector_id, listing_revision_id,
            metric_start, metric_end, bucket_granularity, traffic_source,
            impressions, clicks, visitors, favorites, cart_adds, orders,
            sales_amount, ad_spend, search_impressions, recommend_impressions,
            data_as_of, source_id, payload_hash, version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bucket_id,
            TENANT,
            "connector-1",
            revision,
            f"{day}T00:00:00+00:00",
            f"{day}T23:59:59+00:00",
            "day",
            "search",
            impressions,
            0,
            0,
            0,
            0,
            0,
            "0.00",
            "0.00",
            0,
            0,
            as_of,
            source_id,
            "d" * 64,
            1,
            as_of,
            as_of,
        ),
    )


def test_no_traffic_returns_none(tmp_path) -> None:
    adapter = _signal(tmp_path)
    assert adapter.load(tenant_id=TENANT, store_id=STORE, sku_id=SKU) is None


def test_traffic_produces_actual_series_with_trailing_mean(tmp_path) -> None:
    adapter = _signal(
        tmp_path,
        days=[
            ("2026-08-10", 100, "2026-08-11T00:00:00+00:00"),
            ("2026-08-11", 200, "2026-08-12T00:00:00+00:00"),
            ("2026-08-12", 150, "2026-08-13T00:00:00+00:00"),
        ],
    )
    result = adapter.load(tenant_id=TENANT, store_id=STORE, sku_id=SKU)
    assert result is not None
    assert result.source_kind is SourceKind.ACTUAL
    assert result.data_as_of == date(2026, 8, 13)
    # 首日无历史 → 1.0；次日 200/100=2.0；第三日 150/150=1.0
    assert result.signal_by_date == {
        date(2026, 8, 10): 1.0,
        date(2026, 8, 11): 2.0,
        date(2026, 8, 12): 1.0,
    }


def test_cross_store_and_sku_isolated(tmp_path) -> None:
    db = _make_db(tmp_path / "isolation.sqlite3")
    with db.connect() as conn:
        _seed_revision_and_buckets(
            conn,
            store_id="store-a",
            sku_id="sku-a",
            days=[("2026-08-10", 100, "2026-08-11T00:00:00+00:00")],
        )
        _seed_revision_and_buckets(
            conn,
            store_id="store-b",
            sku_id="sku-b",
            days=[("2026-08-10", 999, "2026-08-11T00:00:00+00:00")],
        )
    adapter = TrafficSignalAdapter(db)

    a = adapter.load(tenant_id=TENANT, store_id="store-a", sku_id="sku-a")
    b = adapter.load(tenant_id=TENANT, store_id="store-b", sku_id="sku-b")
    assert a is not None and list(a.signal_by_date.values()) == [1.0]
    assert b is not None and list(b.signal_by_date.values()) == [1.0]
    # 同店不同 SKU / 跨店 SKU 错配 → 无信号（不串数据）
    assert adapter.load(tenant_id=TENANT, store_id="store-a", sku_id="sku-b") is None
    assert adapter.load(tenant_id=TENANT, store_id="store-b", sku_id="sku-a") is None


def test_revision_as_of_uses_value_visible_at_day_time(tmp_path) -> None:
    db = _make_db(tmp_path / "revision.sqlite3")
    with db.connect() as conn:
        _seed_revision_and_buckets(
            conn,
            days=[
                ("2026-08-10", 100, "2026-08-11T00:00:00+00:00"),
                ("2026-08-11", 200, "2026-08-13T00:00:00+00:00"),
            ],
        )
        # 业务日 08-10 的迟到修订：data_as_of=08-14，晚于 08-11 的可见时间，
        # 不应回改历史因子（否则 200/1000=0.2 泄漏未来信息）。
        _insert_bucket(
            conn,
            bucket_id="bucket-rev",
            revision="rev-store-signal-sku-signal",
            day="2026-08-10",
            impressions=1000,
            as_of="2026-08-14T00:00:00+00:00",
            source_id="src-rev",
        )
    result = TrafficSignalAdapter(db).load(
        tenant_id=TENANT, store_id=STORE, sku_id=SKU
    )
    assert result is not None
    assert result.signal_by_date[date(2026, 8, 10)] == 1.0
    assert result.signal_by_date[date(2026, 8, 11)] == 2.0
    assert result.signal_as_of[date(2026, 8, 10)] == date(2026, 8, 11)


def test_stale_as_of_row_excluded(tmp_path) -> None:
    adapter = _signal(
        tmp_path,
        days=[
            # data_as_of 早于 metric_end：报告时点之前不可见，丢弃
            ("2026-08-10", 100, "2026-08-10T06:00:00+00:00"),
        ],
    )
    assert adapter.load(tenant_id=TENANT, store_id=STORE, sku_id=SKU) is None


def test_field_evidence_demo_downgrades_source_kind(tmp_path) -> None:
    db = _make_db(tmp_path / "demo.sqlite3")
    with db.connect() as conn:
        _seed_revision_and_buckets(
            conn, days=[("2026-08-10", 100, "2026-08-11T00:00:00+00:00")]
        )
    content = b"demo traffic"
    readonly = ReadonlyDataService(db)
    imported = readonly.record_import(
        TENANT,
        ImportManifestInput(
            store_id=STORE,
            source_kind=SourceKind.DEMO,
            source_system="demo",
            report_type="traffic",
            report_period="2026-08",
            exported_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
            schema_fingerprint=schema_fingerprint(["metric_start", "impressions"]),
            content_digest=content_digest(content),
            mapping_version="traffic-v1",
            parsed_rows=1,
            data_as_of=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
            references=[
                ImportReference(
                    kind="raw_file",
                    reference="objects/readonly-imports/demo-traffic.csv",
                    content_digest=content_digest(content),
                )
            ],
        ),
    )
    readonly.record_field_evidence(
        TENANT,
        FieldEvidenceInput(
            store_id=STORE,
            field_key="readiness:traffic_metric_buckets",
            scope="store",
            evidence_state=EvidenceState.DEMO,
            reason="demo_traffic",
            import_id=imported["import_id"],
            source_reference="objects/readonly-imports/demo-traffic.csv",
        ),
    )
    result = TrafficSignalAdapter(db).load(
        tenant_id=TENANT, store_id=STORE, sku_id=SKU
    )
    assert result is not None
    assert result.source_kind is SourceKind.DEMO


class _StubAdapter:
    def __init__(self, result: SignalInput | None) -> None:
        self.result = result

    def load(self, **kwargs: object) -> SignalInput | None:
        return self.result


class _FactSource:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_facts(
        self, tenant_id: str, *, store_id: str, sku_id: str, **_kwargs: object
    ) -> list[dict]:
        return [
            dict(row)
            for row in self.rows
            if row["tenant_id"] == tenant_id
            and row["store_id"] == store_id
            and row["sku_id"] == sku_id
        ]


def _facts() -> list[dict]:
    from datetime import timedelta

    start = date(2026, 1, 1)
    return [
        {
            "id": f"fact-{offset}",
            "tenant_id": TENANT,
            "store_id": STORE,
            "sku_id": SKU,
            "business_date": (start + timedelta(days=offset)).isoformat(),
            "eligible_units": 10,
            "stockout_flag": "false",
            "demand_policy_version": "demand-v1",
            "fact_version": 1,
            "payload_hash": f"hash-{offset}",
            "quality_flags": [],
        }
        for offset in range(56)
    ]


def test_run_service_consumes_adapter_and_rejects_future_leakage(tmp_path) -> None:
    db = _make_db(tmp_path / "leak.sqlite3")
    service = ForecastRunService(
        db,
        facts=_FactSource(_facts()),
        signal_adapter=_StubAdapter(
            SignalInput(
                signal_by_date={date(2026, 12, 31): 1.5},
                source_kind=SourceKind.ACTUAL,
                data_as_of=date(2026, 1, 15),
            )
        ),
    )
    run = service.run(TENANT, store_id=STORE, sku_id=SKU)
    reason = run["candidate_models"]["signal_champion_reason"]
    assert reason["admission"] == "rejected_future_leakage"
    assert reason["signal_usage"] == "not_used"


def test_run_service_rejects_signal_not_better_than_baseline(tmp_path) -> None:
    db = _make_db(tmp_path / "worse.sqlite3")
    service = ForecastRunService(
        db,
        facts=_FactSource(_facts()),
        signal_adapter=_StubAdapter(
            SignalInput(
                signal_by_date={date(2026, 1, 1): 1.0},
                source_kind=SourceKind.ACTUAL,
                data_as_of=date(2026, 2, 1),
            )
        ),
    )
    run = service.run(TENANT, store_id=STORE, sku_id=SKU)
    reason = run["candidate_models"]["signal_champion_reason"]
    assert reason["admission"] == "rejected_not_better"
    assert reason["signal_usage"] == "not_used"
