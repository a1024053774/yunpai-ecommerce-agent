from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _audit_rows(app, event_type: str) -> list[dict]:
    with app.state.agent.db.connect() as conn:
        rows = conn.execute(
            "SELECT event_type, actor, subject_id, tenant_id FROM audit_log "
            "WHERE event_type=?",
            (event_type,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_profit_policy_write_is_audited(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/profit/policies",
            headers=ADMIN_HEADERS,
            json={"policy_version": "v-audit"},
        )
    assert response.status_code == 200
    rows = _audit_rows(app, "profit.policy.registered")
    assert len(rows) == 1
    assert rows[0]["subject_id"] == "v-audit"
    assert rows[0]["tenant_id"] == "tenant-test"


def test_ordering_write_operations_are_audited(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/ordering/drafts",
            headers=ADMIN_HEADERS,
            json={
                "store_id": "store-audit",
                "sku_id": "SKU-A",
                "recommended_qty": 5,
                "source_summary": "audit demo",
                "mode": "demo",
            },
        )
        draft_id = created.json()["order_draft_id"]
        client.post(
            f"/v1/ordering/drafts/{draft_id}/submit?store_id=store-audit",
            headers=ADMIN_HEADERS,
        )
        client.post(
            f"/v1/ordering/drafts/{draft_id}/confirm?store_id=store-audit",
            headers=ADMIN_HEADERS,
            json={"version": 1, "confirmed_qty": 5},
        )

    assert _audit_rows(app, "ordering.draft.created")[0]["subject_id"] == draft_id
    assert _audit_rows(app, "ordering.draft.submitted")[0]["subject_id"] == draft_id
    assert _audit_rows(app, "ordering.draft.confirmed")[0]["subject_id"] == draft_id


def test_final_profit_capability_default_denies(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FINAL_PROFIT_READ_ADMIN_IDS", raising=False)
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/v1/profit/policies",
            headers=ADMIN_HEADERS,
            json={"policy_version": "v-deny"},
        )
        response = client.get(
            "/v1/profit/projection?store_id=store-x&period=2026-08&scope=formal",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["final"]["restricted"] is True
    assert body["final"]["amount"] is None
    assert _audit_rows(app, "profit.final_profit.read_denied")


def test_final_profit_capability_granted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FINAL_PROFIT_READ_ADMIN_IDS", "admin-test")
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        client.post(
            "/v1/profit/policies",
            headers=ADMIN_HEADERS,
            json={"policy_version": "v-grant"},
        )
        response = client.get(
            "/v1/profit/projection?store_id=store-x&period=2026-08&scope=formal",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["final"]["restricted"] is False


def _seed_delivered_order_and_entries(app) -> None:
    with app.state.agent.db.connect() as conn:
        conn.execute(
            """INSERT INTO commerce_orders (
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES (?, 'tenant-test', 'conn-1', 'store-x', ?, 'delivered',
                      'paid', 'CNY', '100.00', '2026-08-01T00:00:00+00:00',
                      '2026-08-01T00:00:00+00:00', ?, 1,
                      '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')""",
            ("order-ledger", "O-LEDGER", "0" * 64),
        )
    with app.state.agent.db.connect() as conn:
        conn.execute(
            """INSERT INTO profit_ledger_entries (
                entry_id, tenant_id, store_id, period, category, scope, amount,
                currency, source_kind, sku_id, order_id, mapping_version,
                entry_key, payload_hash, source_reference, granularity,
                is_estimated, reconciliation_status, created_at
            ) VALUES (?, 'tenant-test', 'store-x', '2026-08', 'signed_receipt_revenue',
                      'formal', '1000.00', 'CNY', 'actual', 'SKU-1', 'O-LEDGER',
                      'v1', 'rev-ledger', ?, NULL, 'order', 0, 'pending',
                      '2026-08-24T00:00:00+00:00')""",
            ("entry-rev", "a" * 64),
        )
        conn.execute(
            """INSERT INTO profit_ledger_entries (
                entry_id, tenant_id, store_id, period, category, scope, amount,
                currency, source_kind, sku_id, order_id, mapping_version,
                entry_key, payload_hash, source_reference, granularity,
                is_estimated, reconciliation_status, created_at
            ) VALUES (?, 'tenant-test', 'store-x', '2026-08', 'tax_cost',
                      'formal', '-20.00', 'CNY', 'manual', 'SKU-1', NULL,
                      'v1', 'tax-ledger', ?, NULL, 'store', 0, 'pending',
                      '2026-08-24T00:00:00+00:00')""",
            ("entry-tax", "b" * 64),
        )


def test_ledger_entries_api_masks_final_without_capability(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("FINAL_PROFIT_READ_ADMIN_IDS", raising=False)
    app = create_app(make_settings(tmp_path))
    _seed_delivered_order_and_entries(app)
    with TestClient(app) as client:
        response = client.get(
            "/v1/profit/ledger/entries?store_id=store-x&sku_id=SKU-1&scope=formal",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    tax = next(row for row in rows if row["category"] == "tax_cost")
    assert tax["amount"] is None
    assert tax["restricted"] is True
    revenue = next(row for row in rows if row["category"] == "signed_receipt_revenue")
    assert revenue["amount"] == "1000.00"
    assert _audit_rows(app, "profit.ledger.entries.final_denied")


def test_ledger_entries_api_shows_final_with_capability(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FINAL_PROFIT_READ_ADMIN_IDS", "admin-test")
    app = create_app(make_settings(tmp_path))
    _seed_delivered_order_and_entries(app)
    with TestClient(app) as client:
        response = client.get(
            "/v1/profit/ledger/entries?store_id=store-x&sku_id=SKU-1&scope=formal",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    tax = next(
        row for row in response.json() if row["category"] == "tax_cost"
    )
    assert tax["amount"] == "-20.00"


def _seed_duplicate_final(app) -> None:
    with app.state.agent.db.connect() as conn:
        conn.execute(
            """INSERT INTO commerce_orders (
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES ('order-dup', 'tenant-test', 'conn-1', 'store-x', 'O-DUP',
                      'delivered', 'paid', 'CNY', '100.00',
                      '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                      'z' * 64, 1, '2026-08-01T00:00:00+00:00',
                      '2026-08-01T00:00:00+00:00')"""
        )
        for entry_id, entry_key in (("dup-rev", "rev-dup"), ("dup-tax-1", "tax-dup-1"), ("dup-tax-2", "tax-dup-2")):
            category = "signed_receipt_revenue" if entry_id == "dup-rev" else "tax_cost"
            amount = "1000.00" if entry_id == "dup-rev" else "-20.00"
            conn.execute(
                """INSERT INTO profit_ledger_entries (
                    entry_id, tenant_id, store_id, period, category, scope, amount,
                    currency, source_kind, sku_id, order_id, mapping_version,
                    entry_key, payload_hash, source_reference, granularity,
                    is_estimated, reconciliation_status, created_at
                ) VALUES (?, 'tenant-test', 'store-x', '2026-08', ?, 'formal', ?,
                          'CNY', 'manual', 'SKU-1', ?, 'v1', ?, ?, NULL, 'store',
                          0, 'pending', '2026-08-24T00:00:00+00:00')""",
                (entry_id, category, amount, "O-DUP" if entry_id == "dup-rev" else None, entry_key, "c" * 64),
            )


def test_reconciliation_api_masks_final_amount_without_capability(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("FINAL_PROFIT_READ_ADMIN_IDS", raising=False)
    app = create_app(make_settings(tmp_path))
    _seed_duplicate_final(app)
    with TestClient(app) as client:
        response = client.get(
            "/v1/profit/reconciliation?store_id=store-x&period=2026-08&scope=formal",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    final_issues = [i for i in response.json()["issues"] if i.get("is_final")]
    assert final_issues
    assert all(i["amount"] is None for i in final_issues)
    assert _audit_rows(app, "profit.reconciliation.final_denied")


def test_ledger_entry_audit_does_not_log_final_amount(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    _seed_delivered_order_and_entries(app)
    with TestClient(app) as client:
        response = client.post(
            "/v1/profit/ledger/entries",
            headers=ADMIN_HEADERS,
            json={
                "store_id": "store-x",
                "period": "2026-08",
                "category": "tax_cost",
                "scope": "formal",
                "amount": "-5.00",
                "source_kind": "manual",
                "sku_id": "SKU-1",
                "entry_key": "tax-audit",
                "granularity": "store",
            },
        )
    assert response.status_code == 200
    import json as _json
    with app.state.agent.db.connect() as conn:
        row = conn.execute(
            """SELECT detail_json FROM audit_log
               WHERE event_type='profit.ledger.entry_recorded'
                 AND subject_id=?""",
            (response.json()["entry_id"],),
        ).fetchone()
    detail = _json.loads(row["detail_json"])
    assert detail["category"] == "tax_cost"
    assert detail["amount"] is None


def _seed_minimal_profit(app) -> None:
    with app.state.agent.db.connect() as conn:
        conn.execute(
            """INSERT INTO commerce_orders (
                id, tenant_id, connector_id, store_id, external_order_id,
                order_status, payment_status, currency, total_amount, placed_at,
                source_updated_at, payload_hash, version, created_at, updated_at
            ) VALUES ('order-min', 'tenant-test', 'conn-1', 'store-x', 'O-MIN',
                      'delivered', 'paid', 'CNY', '100.00',
                      '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                      'm' * 64, 1, '2026-08-01T00:00:00+00:00',
                      '2026-08-01T00:00:00+00:00')"""
        )
        for entry_id, category, amount in (
            ("min-rev", "signed_receipt_revenue", "1000.00"),
            ("min-tax", "tax_cost", "-20.00"),
        ):
            conn.execute(
                """INSERT INTO profit_ledger_entries (
                    entry_id, tenant_id, store_id, period, category, scope, amount,
                    currency, source_kind, sku_id, order_id, mapping_version,
                    entry_key, payload_hash, source_reference, granularity,
                    is_estimated, reconciliation_status, created_at
                ) VALUES (?, 'tenant-test', 'store-x', '2026-08', ?, 'formal', ?,
                          'CNY', 'actual', 'SKU-1', 'O-MIN', 'v1', ?, ?, NULL,
                          'store', 0, 'pending', '2026-08-24T00:00:00+00:00')""",
                (entry_id, category, amount, entry_id + "-key", "n" * 64),
            )


class _FakeAdvisor:
    def __init__(self) -> None:
        self.facts = None

    def suggest(self, facts):
        self.facts = facts
        return type(
            "R",
            (),
            {
                "available": False,
                "reason": "model_unavailable",
                "suggestions": [],
                "facts_digest": "x" * 64,
            },
        )()


def test_decision_api_masks_final_amount_in_model_facts(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FINAL_PROFIT_READ_ADMIN_IDS", raising=False)
    app = create_app(make_settings(tmp_path))
    _seed_minimal_profit(app)
    fake = _FakeAdvisor()
    app.state.agent.decision_advisor = fake
    with TestClient(app) as client:
        client.post(
            "/v1/profit/policies",
            headers=ADMIN_HEADERS,
            json={
                "policy_version": "v-min",
                "required_categories": {
                    "sales": ["signed_receipt_revenue"],
                    "operating": ["signed_receipt_revenue"],
                    "final": ["signed_receipt_revenue", "tax_cost"],
                },
            },
        )
        response = client.post(
            "/v1/decision/suggestions",
            headers=ADMIN_HEADERS,
            json={"store_id": "store-x", "period": "2026-08", "scope": "formal"},
        )
    assert response.status_code == 200
    assert fake.facts is not None
    assert fake.facts["profit_projection"]["final"]["amount"] is None
    assert fake.facts["profit_projection"]["final"]["restricted"] is True


def _seed_legacy_audit_with_final_amount(app) -> None:
    with app.state.agent.db.connect() as conn:
        conn.execute(
            """INSERT INTO audit_log (
                id, event_type, actor, subject_id, detail_json, tenant_id, created_at
            ) VALUES (?, 'profit.ledger.entry_recorded', 'legacy-admin', 'entry-legacy',
                      ?, 'tenant-test', '2026-08-20T00:00:00+00:00')""",
            (
                "audit-legacy-final",
                '{"category": "tax_cost", "amount": "-4321.09", "entry_key": "tax-old"}',
            ),
        )


def test_legacy_audit_final_amount_masked_without_capability(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("FINAL_PROFIT_READ_ADMIN_IDS", raising=False)
    app = create_app(make_settings(tmp_path))
    _seed_legacy_audit_with_final_amount(app)
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/audit?event_type=profit.ledger.entry_recorded",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    events = [e for e in response.json() if e["event_type"] == "profit.ledger.entry_recorded"]
    assert events
    legacy = next(e for e in events if e["subject_id"] == "entry-legacy")
    assert legacy["detail"]["amount"] is None


def test_legacy_audit_final_amount_shown_with_capability(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FINAL_PROFIT_READ_ADMIN_IDS", "admin-test")
    app = create_app(make_settings(tmp_path))
    _seed_legacy_audit_with_final_amount(app)
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/audit?event_type=profit.ledger.entry_recorded",
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 200
    legacy = next(
        e
        for e in response.json()
        if e["event_type"] == "profit.ledger.entry_recorded"
        and e["subject_id"] == "entry-legacy"
    )
    assert legacy["detail"]["amount"] == "-4321.09"
