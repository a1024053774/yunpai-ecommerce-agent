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
