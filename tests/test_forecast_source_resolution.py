from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import OrderUpsert
from ecommerce_agent.business.orders import OrderLineInput

from conftest import make_settings


ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
TENANT = "tenant-test"
SHANGHAI = timezone(timedelta(hours=8))


def _seed_real_orders(service, *, store_id: str, sku_id: str, days: int = 21) -> None:
    for index in range(days):
        business_day = date(2026, 7, 1) + timedelta(days=index)
        placed_at = datetime.combine(business_day, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        service.operations.orders.upsert(
            TENANT,
            OrderUpsert(
                connector_id="forecast-source-test",
                store_id=store_id,
                order_id=f"real-order-{index}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal("99"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"real-line-{index}",
                        sku_id=sku_id,
                        title="Real sales history",
                        quantity=(index % 7) + 1,
                        unit_price=Decimal("99"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"real-source-{index}",
            ),
        )


def test_resolve_and_run_prefers_sufficient_real_sales_history(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        _seed_real_orders(service, store_id="real-store", sku_id="real-sku")
        with TestClient(app) as client:
            response = client.post(
                "/v1/forecasting/resolve-and-run",
                headers=ADMIN_HEADERS,
                json={"store_id": "real-store", "sku_id": "real-sku", "horizon_days": 7},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source_type"] == "real"
        assert body["virtual"] is False
        assert body["effective_scope"] == {"store_id": "real-store", "sku_id": "real-sku"}
        assert len(body["forecast"]["forecast_points"]) == 7
    finally:
        service.close()


def test_resolve_and_run_defaults_to_a_reused_isolated_demo(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        with TestClient(app) as client:
            first = client.post(
                "/v1/forecasting/resolve-and-run",
                headers=ADMIN_HEADERS,
                json={"horizon_days": 30},
            )
            second = client.post(
                "/v1/forecasting/resolve-and-run",
                headers=ADMIN_HEADERS,
                json={"horizon_days": 30},
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_body = first.json()
        second_body = second.json()
        assert first_body["source_type"] == "demo"
        assert first_body["virtual"] is True
        assert first_body["production_claim"] is False
        assert first_body["requested_scope"] is None
        assert first_body["effective_scope"]["store_id"] != ""
        assert first_body["forecast"]["run_id"] == second_body["forecast"]["run_id"]
        assert first_body["demo_sales_day_count"] == 1095
    finally:
        service.close()
