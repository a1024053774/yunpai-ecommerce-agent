from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.workspace_conversations import (
    WorkspaceConversationSummary,
    WorkspaceMessageRecord,
    build_workspace_history,
    derive_workspace_title,
)

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}


def test_workspace_contract_derives_title_and_truncates_history() -> None:
    messages = [
        {"role": "user", "content": f"问题 {index}"}
        for index in range(14)
    ]
    messages.extend(
        [
            {"role": "assistant", "content": "回答"},
            {"role": "tool", "content": "不应进入模型历史"},
        ]
    )

    assert derive_workspace_title(messages[0]["content"]) == "问题 0"
    assert build_workspace_history(messages, limit=12) == [
        {"role": "user", "content": f"问题 {index}"} for index in range(3, 14)
    ] + [{"role": "assistant", "content": "回答"}]


def test_workspace_contract_validates_persisted_records() -> None:
    summary = WorkspaceConversationSummary(
        id="workspace:abc",
        title="库存风险",
        status="active",
        message_count=2,
        created_at="2026-08-12T05:00:00+00:00",
        updated_at="2026-08-12T05:01:00+00:00",
    )
    message = WorkspaceMessageRecord(
        id="workspace-message:1",
        conversation_id=summary.id,
        role="assistant",
        content="已完成库存核对",
        status="completed",
        created_at=summary.updated_at,
        updated_at=summary.updated_at,
        trace_id="trace-1",
        tool_name="get_inventory_risk",
        tool_label="库存风险",
        tool_summary="共核对 6 个商品",
        requires_confirmation=True,
        action_summary="进入高级管理确认补货",
        processing={"stage": "completed", "tool_name": "get_inventory_risk"},
    )

    assert summary.message_count == 2
    assert message.conversation_id == summary.id
    assert message.requires_confirmation is True
    assert message.tool_summary == "共核对 6 个商品"
    assert message.processing["stage"] == "completed"


def test_admin_can_create_a_private_workspace_conversation(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app) as client:
        unauthorized = client.post("/v1/admin/workspace/conversations")
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/admin/workspace/conversations",
            headers=ADMIN_HEADERS,
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "title": "新会话",
        "status": "active",
        "message_count": 0,
        "created_at": response.json()["created_at"],
        "updated_at": response.json()["updated_at"],
    }
    assert response.json()["id"].startswith("workspace:")


def test_workspace_conversations_are_listed_and_messages_are_restored(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        conversation_id = created["id"]
        app.state.agent.db.update_workspace_conversation_title(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation_id,
            title="查看库存风险",
        )
        app.state.agent.db.append_workspace_message(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation_id,
            role="user",
            content="查看库存风险",
        )
        app.state.agent.db.append_workspace_message(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation_id,
            role="assistant",
            content="当前没有缺货风险",
            processing={"tool_label": "库存风险", "tool_summary": "共核对 6 个商品"},
        )

        conversations = client.get(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation_id}/messages",
            headers=ADMIN_HEADERS,
        )

    assert conversations.status_code == 200
    assert conversations.json()[0]["id"] == conversation_id
    assert conversations.json()[0]["title"] == "查看库存风险"
    assert conversations.json()[0]["message_count"] == 2
    assert [item["role"] for item in messages.json()] == ["user", "assistant"]
    assert messages.json()[1]["processing"]["tool_summary"] == "共核对 6 个商品"


def test_list_workspace_messages_returns_newest_within_limit(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    db = app.state.agent.db
    created = db.create_workspace_conversation(
        tenant_id="tenant-test", admin_id="admin-test", title="历史顺序"
    )
    conversation_id = created["id"]
    for index in range(15):
        db.append_workspace_message(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"m{index:02d}",
        )
    messages = db.list_workspace_messages(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=conversation_id,
        limit=12,
    )
    assert [message["content"] for message in messages] == [
        f"m{index:02d}" for index in range(3, 15)
    ]


def test_workspace_persists_redacted_user_message(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "answer",
            "response": "已核对",
            "reason": "测试",
        },
    )
    monkeypatch.setattr(
        app.state.agent.model,
        "stream_generate",
        lambda messages: iter(["已核对"]),
    )
    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "我的手机 13800138000，地址上海测试路 1 号", "context": {}},
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()
    assert "13800138000" not in messages[0]["content"]
    assert "138****8000" in messages[0]["content"]


def test_workspace_conversation_can_be_renamed_and_archived(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        renamed = client.patch(
            f"/v1/admin/workspace/conversations/{conversation['id']}",
            headers=ADMIN_HEADERS,
            json={"title": "改名后的会话"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "改名后的会话"
        archived = client.patch(
            f"/v1/admin/workspace/conversations/{conversation['id']}",
            headers=ADMIN_HEADERS,
            json={"status": "archived"},
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"


def test_recover_stale_generating_marks_generating_incomplete(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    db = app.state.agent.db
    created = db.create_workspace_conversation(
        tenant_id="tenant-test", admin_id="admin-test", title="恢复"
    )
    message = db.append_workspace_message(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=created["id"],
        role="assistant",
        content="",
        status="generating",
        processing={"stage": "generating"},
    )
    recovered = db.recover_stale_generating(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=created["id"],
    )
    assert recovered == 1
    current = db.get_workspace_message(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=created["id"],
        message_id=message["id"],
    )
    assert current["status"] == "incomplete"


def test_workspace_conversation_scope_is_not_disclosed(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        hidden = client.get(
            f"/v1/admin/workspace/conversations/{created['id']}/messages",
            headers={
                "X-Admin-Id": "other-admin",
                "X-Admin-Key": "other-admin-key-123456789",
            },
        )

    assert hidden.status_code in {401, 404}


def test_schema_31_workspace_tables_are_idempotent(tmp_path) -> None:
    from ecommerce_agent.database import Database

    db = Database(tmp_path / "workspace.db")
    db.initialize()
    db.initialize()

    with db.connect() as conn:
        migrations = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert migrations.count(31) == 1
    assert {"workspace_conversations", "workspace_messages"} <= tables


def test_schema_validation_catches_missing_workspace_message_column(
    tmp_path,
) -> None:
    from ecommerce_agent.database import Database

    path = tmp_path / "schema-check.db"
    db = Database(path)
    db.initialize()
    with db.connect() as conn:
        conn.execute("ALTER TABLE workspace_messages DROP COLUMN tool_name")

    failed = False
    try:
        Database(path).initialize()
    except Exception:
        failed = True
    assert failed


def test_workspace_stream_persists_user_answer_and_processing(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    decisions = iter(
        [
            {
                "mode": "answer",
                "response": "当前状态已核对",
                "reason": "测试回答持久化",
            }
        ]
    )
    monkeypatch.setattr(app.state.agent.model, "generate_json", lambda messages, **kwargs: next(decisions))
    monkeypatch.setattr(app.state.agent.model, "stream_generate", lambda messages: iter(["当前状态已核对"]))

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "汇总当前状态", "context": {}},
        )
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    assert events[-1]["event"] == "done"
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "汇总当前状态"
    assert messages[1]["content"] == events[-1]["response"]["answer"]
    assert messages[1]["processing"]["trace_id"] == events[-1]["response"]["trace_id"]


def test_workspace_stream_persists_structured_metadata_columns(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    decisions = iter(
        [
            {
                "mode": "propose_action",
                "response": "确认要执行退款？",
                "action_summary": "执行退款",
                "reason": "用户明确要求退款",
            }
            for _ in range(2)
        ]
    )
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: next(decisions),
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "把这些订单全部退款", "context": {}},
        )

    assert response.status_code == 200
    with app.state.agent.db.connect() as conn:
        row = conn.execute(
            """SELECT trace_id, tool_name, tool_label, tool_summary,
                      requires_confirmation, action_summary, processing_json
               FROM workspace_messages
               WHERE role='assistant'"""
        ).fetchone()
    assert row is not None
    assert row["trace_id"] is not None
    assert row["requires_confirmation"] == 1
    assert "核对后再执行" in row["action_summary"]
    assert "completed" in row["processing_json"]


def test_workspace_messages_restore_structured_metadata_without_processing_json(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_workspace_overview",
                "arguments": {},
                "reason": "先核对当前状态",
            },
            {
                "mode": "propose_action",
                "response": "确认后执行退款",
                "action_summary": "执行退款",
                "reason": "需要改变订单和资金状态",
            },
            {
                "mode": "propose_action",
                "response": "确认后执行退款",
                "action_summary": "执行退款",
                "reason": "需要改变订单和资金状态",
            },
        ]
    )
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: next(decisions),
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "先核对整体状态，再把异常订单退款", "context": {}},
        )
        with app.state.agent.db.connect() as conn:
            conn.execute(
                "UPDATE workspace_messages SET processing_json='{}' "
                "WHERE conversation_id=? AND role='assistant'",
                (conversation["id"],),
            )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    assistant = messages[-1]
    assert assistant["processing"] == {}
    assert assistant["trace_id"].startswith("workspace-")
    assert assistant["tool_name"] == "get_workspace_overview"
    assert assistant["tool_label"] == "经营全局概况"
    assert assistant["tool_summary"]
    assert assistant["requires_confirmation"] is True
    assert assistant["action_summary"] == "该操作需要在对应管理模块核对后再执行"


def test_workspace_message_metadata_updates_only_explicit_fields(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    db = app.state.agent.db
    conversation = db.create_workspace_conversation(
        tenant_id="tenant-test", admin_id="admin-test", title="元数据更新"
    )
    message = db.append_workspace_message(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=conversation["id"],
        role="assistant",
        content="需要确认",
        trace_id="trace-original",
        metadata={
            "tool_name": "get_inventory_risk",
            "tool_label": "库存风险",
            "requires_confirmation": True,
            "action_summary": "确认补货",
        },
    )

    updated = db.update_workspace_message(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=conversation["id"],
        message_id=message["id"],
        status="completed",
        metadata={"tool_summary": "共核对 6 个商品"},
    )

    assert updated["trace_id"] == "trace-original"
    assert updated["tool_name"] == "get_inventory_risk"
    assert updated["requires_confirmation"] is True
    assert updated["tool_summary"] == "共核对 6 个商品"

    cleared = db.update_workspace_message(
        tenant_id="tenant-test",
        admin_id="admin-test",
        conversation_id=conversation["id"],
        message_id=message["id"],
        status="completed",
        metadata={"tool_name": None, "action_summary": None},
    )

    assert cleared["tool_name"] is None
    assert cleared["action_summary"] is None
    assert cleared["requires_confirmation"] is True


def test_workspace_model_answer_not_overridden_by_write_keywords(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    decisions = iter(
        [
            {
                "mode": "answer",
                "response": "退款流程是这样的：售后入口提交申请即可。",
                "reason": "只询问流程",
            }
        ]
    )
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: next(decisions),
    )
    monkeypatch.setattr(
        app.state.agent.model,
        "stream_generate",
        lambda messages: iter(["退款流程是这样的：售后入口提交申请即可。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-no-override-001",
                "message": "不要现在退款，只想了解退款流程",
                "history": [],
                "context": {},
            },
        )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = events[-1]["response"]
    assert done["mode"] == "answer"
    assert done["requires_confirmation"] is False
    assert "退款流程" in done["answer"]


def test_workspace_stream_persists_incomplete_answer_on_agent_error(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: (_ for _ in ()).throw(RuntimeError("planner unavailable")),
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "测试失败恢复", "context": {}},
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["status"] == "incomplete"
    assert messages[-1]["processing"]["stage"] == "error"


def test_workspace_page_uses_server_conversation_history() -> None:
    page = (Path(__file__).parents[1] / "docs" / "agent-workspace.html").read_text(
        encoding="utf-8"
    )

    assert 'id="newConversationButton"' in page
    assert 'id="conversationList"' in page
    assert "/v1/admin/workspace/conversations" in page
    assert "item.tool_label ?? processing.tool_label" in page
    assert "item.requires_confirmation ?? processing.requires_confirmation" in page
    assert "localStorage.setItem(conversationStorageKey()" not in page
