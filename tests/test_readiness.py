from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting.readiness import (
    ReadinessCategory,
    SignalReadinessService,
    TimeGranularity,
)
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


TENANT = "tenant-test"
STORE = "store-test"


def _make_db(path) -> Database:
    db = Database(path)
    db.initialize()
    return db


def _insert_demand_fact(
    conn,
    *,
    sku_id: str = "sku-1",
    fact_id: str | None = None,
    business_date: str = "2026-08-16",
) -> None:
    conn.execute(
        """INSERT INTO demand_daily_facts (
            id, tenant_id, store_id, sku_id, business_date,
            stockout_flag, stockout_evidence_json, promotion_flag,
            source_watermark, fact_version, demand_policy_version,
            quality_flags_json, lineage_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fact_id or f"fact-{sku_id}",
            TENANT,
            STORE,
            sku_id,
            business_date,
            "false",
            "{}",
            "unknown",
            "test",
            1,
            "demand-v1",
            "{}",
            "{}",
            "a" * 64,
            "2026-08-16T00:00:00+00:00",
        ),
    )


def _insert_marketing(conn) -> None:
    conn.execute(
        """INSERT INTO marketing_campaign_metrics (
            id, tenant_id, connector_id, store_id, campaign_id, metric_date,
            campaign_name, channel, objective, status, spend, attributed_revenue,
            attributed_orders, impressions, clicks, source_type, source_updated_at,
            payload_hash, version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "campaign-1",
            TENANT,
            "connector-1",
            STORE,
            "camp-1",
            "2026-08-16",
            "campaign",
            "search",
            "sales",
            "active",
            "10.00",
            "50.00",
            2,
            100,
            10,
            "virtual",
            "2026-08-17T00:00:00+00:00",
            "g" * 64,
            1,
            "2026-08-17T00:00:00+00:00",
            "2026-08-17T00:00:00+00:00",
        ),
    )


def _insert_order_and_after_sale(conn) -> None:
    conn.execute(
        """INSERT INTO commerce_orders (
            id, tenant_id, connector_id, store_id, external_order_id,
            order_status, payment_status, currency, total_amount, placed_at,
            source_updated_at, payload_hash, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "order-1",
            TENANT,
            "connector-1",
            STORE,
            "EXT-1",
            "completed",
            "paid",
            "CNY",
            "100.00",
            "2026-08-15T00:00:00+00:00",
            "2026-08-15T00:00:00+00:00",
            "b" * 64,
            "2026-08-15T00:00:00+00:00",
            "2026-08-15T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO commerce_after_sale_cases (
            id, order_id, external_case_id, case_type, status,
            requested_amount, approved_amount, opened_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "case-1",
            "order-1",
            "CASE-1",
            "refund",
            "approved",
            "100.00",
            "100.00",
            "2026-08-16T00:00:00+00:00",
            "2026-08-16T00:00:00+00:00",
        ),
    )


def _insert_traffic(conn, *, store_id: str = STORE) -> None:
    conn.execute(
        """INSERT INTO creative_assets (
            asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
            feature_schema_version, payload_hash, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "asset-1",
            TENANT,
            "f" * 64,
            "image/png",
            100,
            100,
            "objects/creative/asset-1.png",
            "image-v1",
            "f" * 64,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO listing_revisions (
            id, tenant_id, connector_id, store_id, item_id, sku_id,
            revision_no, title, main_image_asset_id, sale_price,
            attributes_json, active_from, source_updated_at, payload_hash,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "rev-1",
            TENANT,
            "connector-1",
            store_id,
            "item-1",
            "sku-1",
            1,
            "标题",
            "asset-1",
            "10.00",
            "{}",
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
            "c" * 64,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO traffic_metric_buckets (
            id, tenant_id, connector_id, listing_revision_id, metric_start, metric_end,
            bucket_granularity, traffic_source, impressions, clicks, visitors,
            favorites, cart_adds, orders, sales_amount, ad_spend,
            search_impressions, recommend_impressions, data_as_of, source_id,
            payload_hash, version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "bucket-1",
            TENANT,
            "connector-1",
            "rev-1",
            "2026-08-16T00:00:00+00:00",
            "2026-08-16T23:59:59+00:00",
            "day",
            "search",
            100,
            10,
            10,
            1,
            1,
            1,
            "10.00",
            "1.00",
            100,
            0,
            "2026-08-17T00:00:00+00:00",
            "src-1",
            "d" * 64,
            1,
            "2026-08-17T00:00:00+00:00",
            "2026-08-17T00:00:00+00:00",
        ),
    )


def _insert_competitor_observation(conn) -> None:
    conn.execute(
        """INSERT INTO competitor_observations (
            id, tenant_id, connector_id, store_id, subject_sku,
            competitor_name, competitor_sku, subject_price, competitor_price,
            currency, source_type, source_ref, is_estimate, observed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "obs-1",
            TENANT,
            "connector-1",
            STORE,
            "sku-1",
            "竞品A",
            "comp-sku-1",
            "10.00",
            "9.00",
            "CNY",
            "manual",
            "ref-1",
            0,
            "2026-08-16T00:00:00+00:00",
            "2026-08-16T00:00:00+00:00",
        ),
    )


def _insert_approved_match(conn) -> None:
    conn.execute(
        """INSERT INTO competitive_entity_matches (
            id, tenant_id, connector_id, store_id, subject_sku, competitor_name,
competitor_sku, source_type, source_ref, source_id, is_estimate,
observed_at, subject_identity_json, competitor_identity_json, score,
recommended_status, status, payload_hash, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "match-1",
            TENANT,
            "connector-1",
            STORE,
            "sku-1",
            "竞品A",
            "comp-sku-1",
            "manual",
            "ref-1",
            "src-1",
            0,
            "2026-08-16T00:00:00+00:00",
            "{}",
            "{}",
            95,
            "approved",
            "approved",
            "e" * 64,
            "2026-08-16T00:00:00+00:00",
            "2026-08-16T00:00:00+00:00",
        ),
    )


def _manifest(store_id: str) -> ImportManifestInput:
    content = b"order_id,total_amount\nORDER-1,88.00\n"
    digest = content_digest(content)
    return ImportManifestInput(
        store_id=store_id,
        source_kind=SourceKind.ACTUAL,
        source_system="taobao_export",
        report_type="orders",
        report_period="2026-08-16",
        exported_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        schema_fingerprint=schema_fingerprint(["订单编号", "实付金额"]),
        content_digest=digest,
        mapping_version="orders-v1",
        parsed_rows=1,
        data_as_of=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        references=[
            ImportReference(
                kind="raw_file",
                reference=f"objects/readonly-imports/{digest}.csv",
                content_digest=digest,
            )
        ],
    )


def test_empty_store_projects_all_inputs_as_missing_with_reasons(tmp_path) -> None:
    db = _make_db(tmp_path / "empty.sqlite3")
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)

    keys = {item.input_key for item in items}
    assert {
        "demand_daily_facts",
        "traffic_metric_buckets",
        "marketing_campaign_metrics",
        "competitor_approved_signal",
        "after_sale_cases",
        "inventory_balances",
        "inventory_planning_policies",
        "supplier_lead_days",
        "transport_lead_days",
        "catalog_items",
        "material_no_mapping",
    } <= keys
    assert all(item.evidence_state is EvidenceState.MISSING for item in items)
    assert all(item.missing_reason for item in items)


def test_summary_groups_by_category_and_evidence_state(tmp_path) -> None:
    db = _make_db(tmp_path / "summary.sqlite3")
    summary = SignalReadinessService(db).summary(tenant_id=TENANT, store_id=STORE)
    assert summary[ReadinessCategory.FORECAST_TARGET.value]["missing"] == 1
    assert summary[ReadinessCategory.MASTER_DATA.value]["missing"] == 2
    assert summary[ReadinessCategory.DELIVERY_CONSTRAINT.value]["missing"] == 2


def test_demand_facts_mark_actual_with_sku_coverage(tmp_path) -> None:
    db = _make_db(tmp_path / "demand.sqlite3")
    with db.connect() as conn:
        _insert_demand_fact(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    demand = next(item for item in items if item.input_key == "demand_daily_facts")
    assert demand.evidence_state is EvidenceState.ACTUAL
    assert demand.sku_coverage == 1
    assert demand.data_as_of == "2026-08-16T00:00:00"


def test_traffic_marks_actual_with_sku_coverage_via_revision(tmp_path) -> None:
    db = _make_db(tmp_path / "traffic.sqlite3")
    with db.connect() as conn:
        _insert_traffic(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    traffic = next(item for item in items if item.input_key == "traffic_metric_buckets")
    assert traffic.evidence_state is EvidenceState.ACTUAL
    assert traffic.sku_coverage == 1
    assert traffic.granularity is None


def test_sku_coverage_counts_distinct_skus_not_rows(tmp_path) -> None:
    db = _make_db(tmp_path / "sku-distinct.sqlite3")
    with db.connect() as conn:
        _insert_demand_fact(conn, sku_id="sku-1", fact_id="f1", business_date="2026-08-15")
        _insert_demand_fact(conn, sku_id="sku-1", fact_id="f2", business_date="2026-08-16")
        _insert_demand_fact(conn, sku_id="sku-2", fact_id="f3", business_date="2026-08-16")
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    demand = next(item for item in items if item.input_key == "demand_daily_facts")
    assert demand.evidence_state is EvidenceState.ACTUAL
    assert demand.sku_coverage == 2
    assert demand.granularity is TimeGranularity.DAILY


def test_campaign_level_signal_has_no_sku_coverage(tmp_path) -> None:
    db = _make_db(tmp_path / "campaign.sqlite3")
    with db.connect() as conn:
        _insert_marketing(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    marketing = next(
        item for item in items if item.input_key == "marketing_campaign_metrics"
    )
    assert marketing.evidence_state is EvidenceState.ACTUAL
    assert marketing.sku_coverage is None
    assert marketing.granularity is TimeGranularity.DAILY


def test_after_sale_cases_mark_actual(tmp_path) -> None:
    db = _make_db(tmp_path / "after-sale.sqlite3")
    with db.connect() as conn:
        _insert_order_and_after_sale(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    refund = next(item for item in items if item.input_key == "after_sale_cases")
    assert refund.evidence_state is EvidenceState.ACTUAL
    assert refund.data_as_of == "2026-08-16T00:00:00+00:00"


def test_competitor_signal_requires_approved_match(tmp_path) -> None:
    db = _make_db(tmp_path / "competitor.sqlite3")
    with db.connect() as conn:
        _insert_competitor_observation(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    competitor = next(
        item for item in items if item.input_key == "competitor_approved_signal"
    )
    assert competitor.evidence_state is EvidenceState.MISSING

    with db.connect() as conn:
        _insert_approved_match(conn)
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    competitor = next(
        item for item in items if item.input_key == "competitor_approved_signal"
    )
    assert competitor.evidence_state is EvidenceState.ACTUAL
    assert competitor.sku_coverage == 1


def test_missing_field_evidence_overrides_row_presence(tmp_path) -> None:
    db = _make_db(tmp_path / "evidence-missing.sqlite3")
    with db.connect() as conn:
        _insert_demand_fact(conn)
    ReadonlyDataService(db).record_field_evidence(
        TENANT,
        FieldEvidenceInput(
            store_id=STORE,
            field_key="readiness:demand_daily_facts",
            scope="store",
            evidence_state=EvidenceState.MISSING,
            reason="demand_report_not_imported",
        ),
    )
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    demand = next(item for item in items if item.input_key == "demand_daily_facts")
    assert demand.evidence_state is EvidenceState.MISSING
    assert demand.missing_reason == "demand_report_not_imported"


def test_actual_field_evidence_sets_state_and_reference(tmp_path) -> None:
    db = _make_db(tmp_path / "evidence-actual.sqlite3")
    readonly = ReadonlyDataService(db)
    imported = readonly.record_import(TENANT, _manifest(STORE))
    content = b"order_id,total_amount\nORDER-1,88.00\n"
    reference = f"objects/readonly-imports/{content_digest(content)}.csv"
    readonly.record_field_evidence(
        TENANT,
        FieldEvidenceInput(
            store_id=STORE,
            field_key="readiness:demand_daily_facts",
            scope="store",
            evidence_state=EvidenceState.ACTUAL,
            reason="demand_report_imported",
            import_id=imported["import_id"],
            source_reference=reference,
        ),
    )
    items = SignalReadinessService(db).project(tenant_id=TENANT, store_id=STORE)
    demand = next(item for item in items if item.input_key == "demand_daily_facts")
    assert demand.evidence_state is EvidenceState.ACTUAL
    assert demand.source_kind is SourceKind.ACTUAL
    assert demand.source_reference is not None
