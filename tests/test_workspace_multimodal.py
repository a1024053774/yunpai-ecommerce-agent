from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.workspace_presenter import tool_label

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_workspace_capabilities_advertise_customer_service_extensions(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        vision_enabled=True,
        vision_base_url="http://127.0.0.1:58081/v1",
        polish_enabled=True,
        polish_base_url="http://127.0.0.1:58080/v1",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/v1/admin/workspace/capabilities", headers=ADMIN_HEADERS
        )

    assert response.status_code == 200
    customer_service = response.json()["customer_service"]
    assert customer_service["entrypoint"] == "/customer-test"
    assert customer_service["advanced_entrypoint"] == "/admin/advanced"
    assert customer_service["multimodal"]["enabled"] is True
    assert customer_service["polish"]["enabled"] is True


def test_workspace_and_advanced_pages_expose_the_customer_service_entry(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        workspace = client.get("/admin")
        advanced = client.get("/admin/advanced")

    assert workspace.status_code == 200
    assert advanced.status_code == 200
    assert "/customer-test" in workspace.text
    assert "loadCapabilities" in workspace.text
    assert 'id="customerServiceCard"' in workspace.text
    assert 'id="openCustomerTest"' in advanced.text
    assert "图片识别" in advanced.text
    assert "润色" in advanced.text


def test_latest_business_tools_have_customer_facing_labels() -> None:
    assert tool_label("get_demand_forecast") == "需求预测"
    assert tool_label("get_inventory_plan") == "库存计划"
    assert tool_label("list_recommendations") == "商品经营建议"
    assert tool_label("get_recommendation_audit_trail") == "建议审计记录"
