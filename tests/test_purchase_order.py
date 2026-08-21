from __future__ import annotations

from pathlib import Path

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.ordering import (
    OrderDraftCreate,
    OrderDraftMode,
    OrderingError,
    OrderingService,
    OrderConfirmRequest,
    OrderStatusAdvanceRequest,
    PurchaseOrderStatus,
)


TENANT = "tenant-a"
STORE = "store-1"
SKU = "SKU-1"
ACTOR = "admin-1"
H64 = "0" * 64


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    return db


def _seed_import_manifest(conn) -> None:
    conn.execute(
        """
        INSERT INTO readonly_import_manifests (
            import_id, tenant_id, store_id, source_kind, source_system,
            report_type, report_period, exported_at, imported_at,
            schema_fingerprint, content_digest, mapping_version,
            accepted_rows, quarantined_rows, rejected_rows, data_as_of,
            references_json, quality_json, payload_hash
        ) VALUES (?, ?, ?, 'actual', 'test', 'readiness', '2026-08-19',
                  '2026-08-19T04:00:00+00:00', '2026-08-19T04:05:00+00:00',
                  ?, ?, 'v1', 1, 0, 0, '2026-08-19T03:00:00+00:00', '[]',
                  '{"status":"passed"}', ?)
        """,
        ("IMPORT-1", TENANT, STORE, "b" * 64, H64, H64),
    )


def seed_gate_ready(
    db: Database,
    *,
    with_policy: bool = True,
    with_transport: bool = True,
    with_evidence: bool = False,
) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO readonly_canonical_products (
                canonical_product_id, tenant_id, store_id, internal_part_number,
                merchant_code, title, normalized_title, source_kind,
                source_reference, policy_version, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'actual', NULL, 'v1', ?, ?)
            """,
            ("CP-1", TENANT, STORE, "MNO-001", "MC-1", "测试商品", "测试商品",
             H64, "2026-08-19T04:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO readonly_product_mapping_events (
                event_id, tenant_id, store_id, connector_id, sku_id,
                mapping_version, expected_version, event_type,
                canonical_product_id, item_id, merchant_code, decision_key,
                reason, actor_ref, source_import_id, supersedes_event_id,
                policy_version, payload_hash, created_at
            ) VALUES (?, ?, ?, 'conn-1', ?, 1, 0, 'confirmed',
                      'CP-1', 'ITEM-1', 'MC-1', ?, 'seed', 'admin-seed',
                      NULL, NULL, 'v1', ?, ?)
            """,
            ("EV-1", TENANT, STORE, SKU, f"decision-{SKU}", H64,
             "2026-08-19T04:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO forecast_runs (
                run_id, tenant_id, store_id, sku_id, training_start,
                training_end, data_hash, demand_policy_version,
                forecast_policy_version, candidate_models_json, champion_model,
                champion_reason, model_version, wape, bias, smape, rmse,
                forecast_horizon, status, created_at
            ) VALUES (?, ?, ?, ?, '2026-07-01', '2026-08-15', ?, 'v1', 'v1',
                      '[]', 'ewma', 'seed', 'model-v1', 0.2, 0.1, 0.25, 3.1,
                      14, 'completed', ?)
            """,
            ("RUN-1", TENANT, STORE, SKU, H64, "2026-08-19T04:00:00+00:00"),
        )
        if with_transport:
            _seed_import_manifest(conn)
            conn.execute(
                """
                INSERT INTO readonly_field_evidence (
                    evidence_id, tenant_id, store_id, field_key, scope,
                    evidence_state, reason, data_as_of, source_reference,
                    import_id, payload_hash, created_at
                ) VALUES (?, ?, ?, 'readiness:transport_lead_days',
                          'operational', 'actual', 'seed', NULL, NULL,
                          'IMPORT-1', ?, ?)
                """,
                ("FE-T", TENANT, STORE, H64, "2026-08-19T04:00:00+00:00"),
            )
        if with_policy:
            conn.execute(
                """
                INSERT INTO inventory_planning_policies (
                    policy_id, tenant_id, store_id, sku_id, warehouse_id,
                    supplier_lead_days, review_period_days, service_level,
                    minimum_order_qty, order_multiple, minimum_safety_stock,
                    maximum_stock_days, policy_version, active_from, created_at
                ) VALUES (?, ?, ?, ?, NULL, 7, 3, 'p90', '1', '1', '0', 30,
                          'v1', '2026-08-01', ?)
                """,
                ("POL-1", TENANT, STORE, SKU, "2026-08-01T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO inventory_plans (
                    plan_id, tenant_id, store_id, sku_id, forecast_run_id,
                    planning_policy_id, planning_policy_version,
                    inventory_snapshot_json, inventory_snapshot_hash,
                    inventory_as_of, forecast_evidence_json, selected_quantile,
                    on_hand, reserved, inbound, available, reservation_shortfall,
                    future_supply, lead_time_demand, lead_review_demand,
                    reorder_point, target_stock, maximum_stock,
                    recommended_order_qty, quantity_status, quantity_reason,
                    stockout_dates_json, risk_level, risk_evidence_json,
                    overstock_risk, plan_quality, quality_issues_json,
                    assumptions_json, allocation_boundary_json,
                    calculation_steps_json, action_mode, input_hash, created_at
                ) VALUES (?, ?, ?, ?, 'RUN-1', 'POL-1', 'v1',
                          '{}', ?, ?, '{}', 'p50',
                          '10', '1', '2', '10', '0',
                          '0', '12', '12',
                          '12', '30', '60',
                          '12', 'advisory', NULL,
                          '[]', 'low', '{}',
                          0, 'standard', '{}',
                          '{}', '{}',
                          '{}', 'advisory_only', ?, ?)
                """,
                ("PLAN-1", TENANT, STORE, SKU, H64,
                 "2026-08-19T04:00:00+00:00", H64, "2026-08-19T04:00:00+00:00"),
            )
        if with_evidence:
            _seed_import_manifest(conn)
            conn.execute(
                """
                INSERT INTO readonly_field_evidence (
                    evidence_id, tenant_id, store_id, field_key, scope,
                    evidence_state, reason, data_as_of, source_reference,
                    import_id, payload_hash, created_at
                ) VALUES (?, ?, ?, 'readiness:supplier_lead_days',
                          'operational', 'actual', 'seed', NULL, NULL,
                          'IMPORT-1', ?, ?)
                """,
                ("FE-1", TENANT, STORE, H64, "2026-08-19T04:00:00+00:00"),
            )


def make_payload(**overrides) -> OrderDraftCreate:
    values = {
        "store_id": STORE,
        "sku_id": SKU,
        "forecast_run_ref": "RUN-1",
        "policy_ref": "POL-1",
        "recommended_qty": 12,
        "currency": "CNY",
        "source_summary": "补货建议测试",
        "assumptions": ["demo=formal 一致"],
    }
    values.update(overrides)
    return OrderDraftCreate(**values)


def service_for(db: Database) -> OrderingService:
    return OrderingService(db)


def test_formal_gate_blocks_when_material_no_missing(tmp_path) -> None:
    db = make_db(tmp_path)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(TENANT, STORE, ACTOR, make_payload())
    assert "material_no" in str(exc.value)
    assert service.list(TENANT, STORE) == []


def test_formal_gate_blocks_when_forecast_evidence_missing(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(
            TENANT,
            STORE,
            ACTOR,
            make_payload(material_no="MNO-001", forecast_run_ref="RUN-NOPE"),
        )
    assert "forecast_run_ref" in str(exc.value)


def test_formal_gate_blocks_when_supply_constraint_missing(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db, with_policy=False)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(
            TENANT,
            STORE,
            ACTOR,
            make_payload(material_no="MNO-001", policy_ref=None),
        )
    assert "supply_constraint" in str(exc.value)


def test_formal_gate_blocked_reports_all_missing_fields(tmp_path) -> None:
    db = make_db(tmp_path)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(
            TENANT, STORE, ACTOR, make_payload(forecast_run_ref=None, policy_ref=None)
        )
    detail = str(exc.value)
    assert "material_no" in detail
    assert "forecast_run_ref" in detail
    assert "supply_constraint" in detail
    assert "delivery_constraint" in detail


def test_formal_gate_blocks_forged_material_no(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(
            TENANT,
            STORE,
            ACTOR,
            make_payload(material_no="FORGED-NO-SUCH"),
        )
    assert "material_no" in str(exc.value)


def test_formal_gate_blocks_forecast_run_sku_mismatch(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE forecast_runs SET sku_id='OTHER-SKU'
            WHERE tenant_id=? AND store_id=? AND run_id=?
            """,
            (TENANT, STORE, "RUN-1"),
        )
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(TENANT, STORE, ACTOR, make_payload(material_no="MNO-001"))
    assert "forecast_run_ref" in str(exc.value)


def test_formal_gate_blocks_missing_transport_lead(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db, with_transport=False)
    service = service_for(db)
    with pytest.raises(OrderingError) as exc:
        service.create_draft(TENANT, STORE, ACTOR, make_payload(material_no="MNO-001"))
    assert "delivery_constraint" in str(exc.value)


def test_demo_draft_allowed_with_labels_even_without_formal_data(tmp_path) -> None:
    db = make_db(tmp_path)
    service = service_for(db)
    draft = service.create_draft(
        TENANT,
        STORE,
        ACTOR,
        make_payload(mode=OrderDraftMode.DEMO, forecast_run_ref=None, policy_ref=None),
    )
    assert draft.mode is OrderDraftMode.DEMO
    assert draft.unsent_label == "未发送（演示参数）"
    assert draft.status is PurchaseOrderStatus.DRAFT
    assert "formal_data_placeholder" in draft.missing_fields


def test_formal_draft_created_when_gate_passed(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    assert draft.status is PurchaseOrderStatus.DRAFT
    assert draft.version == 1
    assert draft.material_no == "MNO-001"
    assert draft.recommended_qty == 12
    assert draft.confirmed_qty is None
    assert draft.unsent_label == "未发送"
    assert draft.mode is OrderDraftMode.FORMAL
    assert len(draft.events) == 1


def test_draft_cannot_auto_confirm(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    assert draft.status is PurchaseOrderStatus.DRAFT
    with pytest.raises(OrderingError) as exc:
        service.confirm(
            TENANT,
            STORE,
            draft.order_draft_id,
            ACTOR,
            OrderConfirmRequest(version=1, confirmed_qty=5),
        )
    assert "ordering_status_transition_invalid" in str(exc.value)


def test_confirm_requires_matching_version(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    submitted = service.submit_for_confirmation(
        TENANT, STORE, draft.order_draft_id, ACTOR
    )
    assert submitted.status is PurchaseOrderStatus.AWAITING_CONFIRMATION
    with pytest.raises(OrderingError) as exc:
        service.confirm(
            TENANT,
            STORE,
            draft.order_draft_id,
            ACTOR,
            OrderConfirmRequest(version=99, confirmed_qty=5),
        )
    assert "ordering_version_conflict" in str(exc.value)


def test_confirm_creates_new_version_and_keeps_recommended(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    confirmed = service.confirm(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderConfirmRequest(version=1, confirmed_qty=5, promised_delivery_at="2026-09-01"),
    )
    assert confirmed.status is PurchaseOrderStatus.CONFIRMED
    assert confirmed.version == 2
    assert confirmed.confirmed_qty == 5
    assert confirmed.recommended_qty == 12
    assert confirmed.confirmed_by == ACTOR
    assert confirmed.promised_delivery_at == "2026-09-01"


def test_illegal_state_transitions_rejected(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    with pytest.raises(OrderingError) as exc:
        service.advance_status(
            TENANT,
            STORE,
            draft.order_draft_id,
            ACTOR,
            OrderStatusAdvanceRequest(
                version=1,
                to_status=PurchaseOrderStatus.IN_TRANSIT,
                source_ref="shipping-1",
            ),
        )
    assert "ordering_status_transition_invalid" in str(exc.value)


def test_external_states_require_source_ref(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    confirmed = service.confirm(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderConfirmRequest(version=1, confirmed_qty=5),
    )
    with pytest.raises(OrderingError) as exc:
        service.advance_status(
            TENANT,
            STORE,
            draft.order_draft_id,
            ACTOR,
            OrderStatusAdvanceRequest(
                version=confirmed.version,
                to_status=PurchaseOrderStatus.IN_TRANSIT,
            ),
        )
    assert "ordering_external_state_requires_source" in str(exc.value)
    in_transit = service.advance_status(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderStatusAdvanceRequest(
            version=confirmed.version,
            to_status=PurchaseOrderStatus.IN_TRANSIT,
            source_ref="carrier-BILL-1",
        ),
    )
    assert in_transit.status is PurchaseOrderStatus.IN_TRANSIT
    received = service.advance_status(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderStatusAdvanceRequest(
            version=in_transit.version,
            to_status=PurchaseOrderStatus.RECEIVED,
            source_ref="receipt-1",
        ),
    )
    assert received.status is PurchaseOrderStatus.RECEIVED
    assert received.events[-1].source_ref == "receipt-1"


def test_overdue_from_confirmed_with_source(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    confirmed = service.confirm(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderConfirmRequest(version=1, confirmed_qty=5),
    )
    overdue = service.advance_status(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderStatusAdvanceRequest(
            version=confirmed.version,
            to_status=PurchaseOrderStatus.OVERDUE,
            source_ref="carrier-note-1",
        ),
    )
    assert overdue.status is PurchaseOrderStatus.OVERDUE


def test_cancel_from_draft(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    cancelled = service.cancel(TENANT, STORE, draft.order_draft_id, ACTOR)
    assert cancelled.status is PurchaseOrderStatus.CANCELLED
    with pytest.raises(OrderingError) as exc:
        service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    assert "ordering_status_transition_invalid" in str(exc.value)


def test_double_submit_conflict(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    with pytest.raises(OrderingError) as exc:
        service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    assert (
        "ordering_status_conflict" in str(exc.value)
        or "ordering_status_transition_invalid" in str(exc.value)
    )
    view = service.get(TENANT, STORE, draft.order_draft_id)
    assert view.status is PurchaseOrderStatus.AWAITING_CONFIRMATION
    assert len(view.events) == 2  # 创建 + 一次提交，无重复事件


def test_status_updates_never_touch_other_tables(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    service.submit_for_confirmation(TENANT, STORE, draft.order_draft_id, ACTOR)
    service.confirm(
        TENANT,
        STORE,
        draft.order_draft_id,
        ACTOR,
        OrderConfirmRequest(version=1, confirmed_qty=5),
    )
    with db.connect() as conn:
        inventory_rows = conn.execute(
            "SELECT COUNT(*) FROM inventory_plans"
        ).fetchone()[0]
        run_rows = conn.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0]
        order_rows = conn.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0]
        draft_rows = conn.execute(
            "SELECT COUNT(*) FROM purchase_order_drafts"
        ).fetchone()[0]
    assert inventory_rows == 1
    assert run_rows == 1
    assert order_rows == 0
    assert draft_rows == 1


def test_tenant_store_isolation(tmp_path) -> None:
    db = make_db(tmp_path)
    seed_gate_ready(db)
    service = service_for(db)
    draft = service.create_draft(TENANT, STORE, ACTOR, make_payload())
    with pytest.raises(OrderingError) as exc:
        service.get("tenant-b", STORE, draft.order_draft_id)
    assert "ordering_draft_not_found" in str(exc.value)
    assert service.list(TENANT, "store-2") == []
    assert [item.order_draft_id for item in service.list(TENANT, STORE)] == [
        draft.order_draft_id
    ]
