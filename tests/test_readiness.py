from __future__ import annotations

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting.readiness import (
    ReadinessCategory,
    SignalReadinessService,
)
from ecommerce_agent.readonly_data.contracts import EvidenceState


def _make_db(path) -> Database:
    db = Database(path)
    db.initialize()
    return db


def test_empty_store_projects_all_inputs_as_missing_with_reasons(tmp_path) -> None:
    db = _make_db(tmp_path / "empty.sqlite3")
    items = SignalReadinessService(db).project(
        tenant_id="tenant-test", store_id="store-test"
    )

    keys = {item.input_key for item in items}
    assert {
        "demand_daily_facts",
        "traffic_metric_buckets",
        "marketing_campaign_metrics",
        "competitor_observations",
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
    summary = SignalReadinessService(db).summary(
        tenant_id="tenant-test", store_id="store-test"
    )
    assert summary[ReadinessCategory.FORECAST_TARGET.value]["missing"] == 1
    assert summary[ReadinessCategory.MASTER_DATA.value]["missing"] == 2


def test_demand_facts_mark_actual_with_sku_coverage(tmp_path) -> None:
    db = _make_db(tmp_path / "seeded.sqlite3")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO demand_daily_facts (
                id, tenant_id, store_id, sku_id, business_date,
                stockout_flag, stockout_evidence_json, promotion_flag,
                source_watermark, fact_version, demand_policy_version,
                quality_flags_json, lineage_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "fact-1",
                "tenant-test",
                "store-test",
                "sku-1",
                "2026-08-16",
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

    items = SignalReadinessService(db).project(
        tenant_id="tenant-test", store_id="store-test"
    )
    demand = next(item for item in items if item.input_key == "demand_daily_facts")
    assert demand.evidence_state is EvidenceState.ACTUAL
    assert demand.sku_coverage == 1
    assert demand.data_as_of == "2026-08-16T00:00:00"
