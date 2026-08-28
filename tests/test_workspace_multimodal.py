import base64
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.schemas import ChatMessageContent
from ecommerce_agent.vision import VisionResult
from ecommerce_agent.workspace_agent import WorkspaceMessageContent
from ecommerce_agent.workspace_presenter import tool_label

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}

_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
    workspace_multimodal = response.json()["workspace_multimodal"]
    assert workspace_multimodal["entrypoint"] == "/admin"
    assert workspace_multimodal["paste_supported"] is True
    assert workspace_multimodal["optional_file_selection"] is True
    assert "image/png" in workspace_multimodal["image_mime_types"]
    assert workspace_multimodal["max_image_bytes"] > 0
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


def test_workspace_accepts_pasted_image_and_passes_only_observation_to_planner(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        vision_enabled=True,
        vision_base_url="http://127.0.0.1:58081/v1",
        vision_model_name="vision-test",
    )
    app = create_app(settings)
    service = app.state.agent
    planner_payloads: list[dict] = []

    monkeypatch.setattr(
        service.vision,
        "describe",
        lambda **_: VisionResult(
            description="图片可见一个白色电器包装盒。",
            status="applied",
            applied=True,
            latency_ms=17,
            model="vision-test",
            image_count=1,
        ),
    )

    def plan(messages, **_kwargs):
        planner_payloads.append(json.loads(messages[-1]["content"]))
        return {
            "mode": "answer",
            "response": "已读取图片，可继续核对对应经营事实。",
            "reason": "已收到图片观察",
        }

    monkeypatch.setattr(service.model, "generate_json", plan)

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "message": "请根据这张图片确认商品信息",
                "context": {},
                "image": {
                    "mime_type": "image/png",
                    "data_base64": _TINY_PNG,
                },
            },
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    vision = next(event for event in events if event["event"] == "vision")
    assert vision["status"] == "applied"
    done = events[-1]["response"]
    assert done["image_attached"] is True
    assert done["vision_status"] == "applied"
    assert done["vision_model"] == "vision-test"
    assert planner_payloads[0]["image_observation"]["status"] == "applied"
    assert planner_payloads[0]["image_observation"]["evidence"]["description"] == (
        "图片可见一个白色电器包装盒。"
    )
    assert _TINY_PNG not in json.dumps(planner_payloads, ensure_ascii=False)
    assert messages[0]["content"].startswith("（已粘贴图片）")
    assert messages[0]["processing"]["image_attached"] is True
    assert messages[0]["processing"]["vision_evidence"]["semantic_authority"] is False
    assert _TINY_PNG not in json.dumps(messages, ensure_ascii=False)


def test_workspace_persists_non_authoritative_vision_observation_for_next_turn(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_mock_mode=False,
        vision_enabled=True,
        vision_model_name="vision-test",
    )
    app = create_app(settings)
    service = app.state.agent
    planner_payloads: list[dict] = []
    monkeypatch.setattr(
        service.vision,
        "describe",
        lambda **_: VisionResult(
            description="图片可见白色空气炸锅包装。",
            status="applied",
            applied=True,
            latency_ms=7,
            model="vision-test",
            image_count=1,
        ),
    )

    def plan(messages, **_kwargs):
        planner_payloads.append(json.loads(messages[-1]["content"]))
        return {
            "mode": "answer",
            "response": "已收到",
            "reason": "已整理",
        }

    monkeypatch.setattr(service.model, "generate_json", plan)
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=headers
        ).json()
        conversation_id = conversation["id"]
        first = client.post(
            f"/v1/admin/workspace/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={
                "message": "看这张图",
                "image": {"mime_type": "image/png", "data_base64": _TINY_PNG},
            },
        )
        second = client.post(
            f"/v1/admin/workspace/conversations/{conversation_id}/chat/stream",
            headers=headers,
            json={"message": "基于刚才图片继续说明"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(planner_payloads) == 2
    second_history = planner_payloads[-1]["recent_history"]
    assert any(
        "图片观察" in item["content"]
        and "图片可见白色空气炸锅包装" in item["content"]
        for item in second_history
    )
    assert _TINY_PNG not in json.dumps(second_history, ensure_ascii=False)


def test_workspace_image_only_request_is_valid_but_empty_request_is_rejected(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {
            "mode": "answer",
            "response": "图片已收到。",
            "reason": "已收到图片",
        },
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        image_response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "message": "",
                "image": {
                    "mime_type": "image/png",
                    "data_base64": _TINY_PNG,
                },
            },
        )
        empty_response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": ""},
        )

    assert image_response.status_code == 200
    assert empty_response.status_code == 422


def test_customer_and_workspace_chat_share_the_same_image_envelope() -> None:
    payload = {"message": "", "image": {"mime_type": "image/png", "data_base64": _TINY_PNG}}

    customer = ChatMessageContent.model_validate(payload)
    workspace = WorkspaceMessageContent.model_validate(payload)

    assert customer.image is not None
    assert workspace.image is not None
    assert customer.image.mime_type == workspace.image.mime_type == "image/png"
