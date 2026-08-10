from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


def test_demo_plan_returns_virtual_inventory_projection_and_draft(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        with TestClient(app) as client:
            first = client.post(
                "/v1/forecasting/demo-plan",
                headers=ADMIN_HEADERS,
                json={"horizon_days": 30},
            )
            second = client.post(
                "/v1/forecasting/demo-plan",
                headers=ADMIN_HEADERS,
                json={"horizon_days": 30},
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        body = first.json()
        assert body["virtual"] is True
        assert body["production_claim"] is False
        assert body["inventory"]["warehouse_id"]
        assert body["inventory"]["reserved"] == "6"
        assert body["plan"]["status"] == "draft"
        assert body["plan"]["external_order_created"] is False
        assert len(body["plan"]["inventory_projection"]["days"]) == 30
        assert body["plan"]["recommended_order_qty"] != "0.00"
        assert body["plan"]["plan_id"] == second.json()["plan"]["plan_id"]
    finally:
        service.close()
