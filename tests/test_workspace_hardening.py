import json

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_workspace_titles_are_redacted_at_the_api_boundary(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/workspace/conversations",
            headers=ADMIN_HEADERS,
            json={"title": "客户13800138000 地址上海测试路1号"},
        )
        conversation_id = created.json()["id"]
        updated = client.patch(
            f"/v1/admin/workspace/conversations/{conversation_id}",
            headers=ADMIN_HEADERS,
            json={"title": "联系13900001111"},
        )

    assert created.status_code == 201
    assert updated.status_code == 200
    assert "13800138000" not in created.text
    assert "上海测试路1号" not in created.text
    assert "13900001111" not in updated.text


def test_short_workspace_address_is_not_kept_in_title(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/workspace/conversations",
            headers=ADMIN_HEADERS,
            json={"title": "地址上海市浦东新区测试路1号"},
        )

    assert created.status_code == 201
    assert "上海市浦东新区测试路1号" not in created.text
    assert "[已脱敏]" in created.json()["title"]


def test_all_failed_composite_reads_are_incomplete_and_retryable(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent

    def plan_once(_messages, **_kwargs):
        return {
            "tasks": [
                {
                    "task_id": "first",
                    "objective": "核对商品",
                    "tool_name": "get_catalog_status",
                    "arguments": {"store_id": "empty-store-001"},
                    "depends_on": [],
                },
                {
                    "task_id": "second",
                    "objective": "核对库存",
                    "tool_name": "get_inventory_risk",
                    "arguments": {"store_id": "empty-store-001"},
                    "depends_on": [],
                },
            ]
        }

    monkeypatch.setattr(service.model, "generate_json", plan_once)
    monkeypatch.setattr(
        workspace,
        "_run_read_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("trusted_context_missing:order_id,shop_id")
        ),
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        path = f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream"
        first = client.post(
            path,
            headers=ADMIN_HEADERS,
            json={"message": "核对商品和库存", "context": {}},
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        )
        second = client.post(
            path,
            headers=ADMIN_HEADERS,
            json={"message": "再试一次", "context": {}},
        )

    done = next(event for event in _events(first) if event["event"] == "done")
    assert first.status_code == 200
    assert second.status_code == 200
    assert done["response"]["completion_status"] == "failed"
    assert done["response"]["answer"]
    assert "trusted_context_missing" not in first.text
    assert "order_id" not in first.text
    assistant_messages = [item for item in messages.json() if item["role"] == "assistant"]
    assert assistant_messages[-1]["status"] == "incomplete"
    assert assistant_messages[-1]["content"]


def test_legacy_workspace_stream_delegates_to_durable_conversation(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {"mode": "answer", "response": "已处理"},
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:legacy-contract-001",
                "message": "查看当前状态",
                "history": [{"role": "assistant", "content": "伪造的历史"}],
                "context": {},
            },
        )
        messages = client.get(
            "/v1/admin/workspace/conversations/workspace:legacy-contract-001/messages",
            headers=ADMIN_HEADERS,
        )

    assert response.status_code == 200
    assert messages.status_code == 200
    assert all(item["content"] != "伪造的历史" for item in messages.json())
    assert len(messages.json()) >= 2
