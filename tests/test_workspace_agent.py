from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import OrderUpsert
from ecommerce_agent.business.inventory import InventoryBalanceUpsert
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.llm import ModelError, ModelUnavailableError
from ecommerce_agent.workspace_read_plan import WorkspaceTaskResult

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


def test_workspace_composite_inventory_and_revenue_uses_one_read_plan(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    source_time = datetime.now(UTC) - timedelta(days=1)
    for index in range(10):
        service.operations.inventory.upsert(
            "tenant-test",
            InventoryBalanceUpsert(
                connector_id="composite-fixture",
                store_id="qingchuan-flagship-001",
                warehouse_id="warehouse-001",
                sku_id=f"QC-COMPOSITE-{index:02d}",
                on_hand="1" if index < 4 else "100",
                average_daily_sales="10" if index < 4 else "1",
                source_updated_at=source_time,
                source_id=f"inventory-composite-{index:02d}",
            ),
        )
    service.operations.orders.upsert(
        "tenant-test",
        OrderUpsert(
            connector_id="composite-fixture",
            store_id="qingchuan-flagship-001",
            order_id="ORDER-COMPOSITE-001",
            order_status="paid",
            payment_status="paid",
            total_amount="4181.00",
            placed_at=source_time,
            lines=[
                OrderLineInput(
                    line_id="line-composite-001",
                    sku_id="QC-COMPOSITE-00",
                    title="Composite fixture product",
                    quantity=1,
                    unit_price="4181.00",
                )
            ],
            source_updated_at=source_time,
            source_id="order-composite-001",
        ),
    )
    planning_calls: list[list[dict[str, str]]] = []

    def plan_once(messages, **_kwargs):
        planning_calls.append(messages)
        return {
            "tasks": [
                {
                    "task_id": "inventory",
                    "objective": "核对库存风险",
                    "tool_name": "get_inventory_risk",
                    "arguments": {"store_id": "qingchuan-flagship-001"},
                    "depends_on": [],
                },
                {
                    "task_id": "revenue",
                    "objective": "核对已支付收入",
                    "tool_name": "get_business_metric",
                    "arguments": {
                        "metric": "gross_revenue",
                        "store_id": "qingchuan-flagship-001",
                    },
                    "depends_on": [],
                },
            ]
        }

    monkeypatch.setattr(service.model, "generate_json", plan_once)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(
            ["共检查 10 个库存记录，其中 4 个需要优先关注；", "已支付收入为 4181.00 元。"]
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-composite-inventory-revenue-001",
                "message": "查看库存风险和最近收入。",
                "history": [],
                "context": {"store_id": "qingchuan-flagship-001"},
            },
        )

    assert response.status_code == 200
    events = _events(response)
    done = events[-1]["response"]
    assert len(planning_calls) == 1
    assert [item["tool_name"] for item in done["tools_used"]] == [
        "get_inventory_risk",
        "get_business_metric",
    ]
    assert done["completion_status"] == "completed"
    assert [item["status"] for item in done["task_results"]] == [
        "success",
        "success",
    ]
    assert "10" in done["answer"]
    assert "4" in done["answer"]
    assert "4181.00" in done["answer"]


def test_workspace_composite_partial_failure_does_not_turn_failure_into_zero(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {
            "tasks": [
                {
                    "task_id": "inventory",
                    "objective": "核对库存风险",
                    "tool_name": "get_inventory_risk",
                    "arguments": {},
                    "depends_on": [],
                },
                {
                    "task_id": "revenue",
                    "objective": "核对最近收入",
                    "tool_name": "get_business_metric",
                    "arguments": {"metric": "gross_revenue"},
                    "depends_on": [],
                },
            ]
        },
    )

    def run_task(task, *_args):
        if task.task_id == "revenue":
            raise ValueError("metric_source_unavailable")
        return WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="库存风险",
            status="success",
            verified_facts=["共检查 10 个库存记录，其中 4 个需要优先关注。"],
            critical_values=["10", "4"],
        )

    monkeypatch.setattr(workspace, "_run_read_task", run_task)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(
            ["共检查 10 个库存记录，其中 4 个需要优先关注；收入为 0 元。"]
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-composite-partial-001",
                "message": "查看库存风险和最近收入。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert done["completion_status"] == "partial"
    assert [item["status"] for item in done["task_results"]] == [
        "success",
        "failed",
    ]
    assert "10" in done["answer"]
    assert "4" in done["answer"]
    assert "收入为 0" not in done["answer"]
    assert "核对最近收入" in done["answer"]
    assert "暂时无法判断" in done["answer"]


def test_workspace_composite_rejects_answer_that_changes_verified_amount(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {
            "tasks": [
                {
                    "task_id": "revenue",
                    "objective": "核对最近收入",
                    "tool_name": "get_business_metric",
                    "arguments": {"metric": "gross_revenue"},
                    "depends_on": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        workspace,
        "_run_read_task",
        lambda task, *_args: WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="经营指标",
            status="success",
            verified_facts=["已支付且未取消订单金额为 4181.00 元。"],
            critical_values=["4181.00"],
        ),
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["最近收入为 4811.00 元。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-composite-number-guard-001",
                "message": "查看最近收入。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert "4811.00" not in done["answer"]
    assert "4181.00" in done["answer"]
    assert "critical_value_mismatch" in done["degraded_reasons"]


def test_workspace_composite_no_data_cannot_be_rewritten_as_zero(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {
            "tasks": [
                {
                    "task_id": "revenue",
                    "objective": "核对最近收入",
                    "tool_name": "get_business_metric",
                    "arguments": {"metric": "gross_revenue"},
                    "depends_on": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        workspace,
        "_run_read_task",
        lambda task, *_args: WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="经营指标",
            status="no_data",
            verified_facts=["当前查询范围内暂无数据。"],
        ),
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["最近收入为 0.00 元。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-composite-no-data-001",
                "message": "查看最近收入。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert "0.00" not in done["answer"]
    assert "暂无数据" in done["answer"]


def test_workspace_replaces_default_admin_page_and_preserves_advanced_console(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        workspace = client.get("/admin")
        assert workspace.status_code == 200
        assert 'data-app="yunpai-agent-workspace"' in workspace.text
        assert "/v1/admin/workspace/chat/stream" in workspace.text
        assert "今天想把什么交给我" in workspace.text
        assert "今日态势" in workspace.text
        assert "需要你关注" in workspace.text
        assert "查看处理过程" in workspace.text
        assert "查看执行轨迹" not in workspace.text
        assert "追踪：</strong>" not in workspace.text
        assert "result.tool_name" not in workspace.text
        assert "/admin/advanced" in workspace.text

        advanced = client.get("/admin/advanced")
        assert advanced.status_code == 200
        assert "yunpai-admin-console" in advanced.text


def test_workspace_capabilities_are_authenticated_and_read_first(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/v1/admin/workspace/capabilities").status_code == 401
        response = client.get(
            "/v1/admin/workspace/capabilities", headers=ADMIN_HEADERS
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "read_first"
        assert payload["automatic_writes"] is False
        names = {item["name"] for item in payload["tools"]}
        assert {
            "get_workspace_overview",
            "get_customer_service_status",
            "get_governance_status",
            "get_channel_status",
            "get_product_facts",
            "get_order_facts",
            "get_inventory_risk",
            "get_marketing_diagnosis",
            "get_profit_reconciliation",
            "get_operations_assistant_report",
            "generate_marketing_copy_draft",
        } <= names


def test_workspace_stream_routes_to_real_overview_and_exposes_progress(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_workspace_overview",
            "arguments": {},
            "reason": "汇总经营与系统状态",
        },
    )
    response_messages = []

    def stream_generate(messages):
        response_messages.extend(messages)
        return iter(["当前运行正常。", "暂时没有需要立即处理的异常。"])

    monkeypatch.setattr(service.model, "stream_generate", stream_generate)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-overview-001",
                "message": "现在整体运行怎么样？",
                "history": [],
                "context": {},
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)
    names = [event["event"] for event in events]
    assert names[:2] == ["status", "status"]
    assert "meta" in names
    assert "tool" in names
    assert names[-1] == "done"
    assert [event["stage"] for event in events if event["event"] == "status"] == [
        "accepted",
        "planning",
        "observing",
        "composing",
    ]
    streamed = "".join(event["text"] for event in events if event["event"] == "delta")
    done = events[-1]["response"]
    assert streamed == "当前运行正常。暂时没有需要立即处理的异常。"
    assert done["answer"] == streamed
    assert done["tool_name"] == "get_workspace_overview"
    assert done["tool_label"] == "经营全局概况"
    assert done["requires_confirmation"] is False
    assert done["trace_id"].startswith("workspace-")
    answer_payload = response_messages[-1]["content"]
    assert '"已核实结果"' in answer_payload
    assert '"verified_result"' not in answer_payload
    assert '"readiness"' not in answer_payload
    assert '"operators"' not in answer_payload


def test_workspace_customer_service_progress_uses_product_language(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    captured_messages = []
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_customer_service_status",
            "arguments": {"scope": "operational"},
            "reason": "查看客服团队当前接待情况",
        },
    )

    def stream_generate(messages):
        captured_messages.extend(messages)
        return iter(["客服团队当前没有在线客服，也没有正在处理的接待任务。"])

    monkeypatch.setattr(service.model, "stream_generate", stream_generate)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-service-product-language-001",
                "message": "现在总共有几个客服，在线和在工作的各有几个？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    tool_event = next(event for event in events if event["event"] == "tool")
    assert tool_event["tool_label"] == "客服与接待情况"
    assert "总共 1 位客服，在线 1 位，正在工作 0 位" in tool_event["summary"]
    prompt_payload = captured_messages[-1]["content"]
    assert "总共 1 位客服，在线 1 位，正在工作 0 位" in prompt_payload
    assert '"total"' not in prompt_payload
    assert '"active"' not in prompt_payload
    assert '"available"' not in prompt_payload


def test_workspace_falls_back_when_stream_fails_before_any_delta(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_workspace_overview",
            "arguments": {},
            "reason": "核对整机状态",
        },
    )

    def broken_stream(messages):
        raise ModelError("empty stream")
        yield ""

    monkeypatch.setattr(service.model, "stream_generate", broken_stream)
    monkeypatch.setattr(service.model, "generate", lambda messages: "整机状态已经核实。")

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-stream-fallback-001",
                "message": "现在系统怎么样？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert any(
        event.get("stage") == "composing_fallback"
        for event in events
        if event["event"] == "status"
    )
    assert not any(event["event"] == "error" for event in events)
    assert events[-1]["response"]["answer"] == "整机状态已经核实。"


def test_workspace_never_executes_write_requests_without_confirmation(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "propose_action",
            "tool_name": None,
            "arguments": {},
            "response": "我可以为这批订单发起退款，但需要你在高级管理中确认。",
            "reason": "退款会改变订单和资金状态",
            "action_summary": "为筛选出的订单发起退款",
            "advanced_view": "orders",
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-action-001",
                "message": "把这些订单全部退款",
                "history": [],
                "context": {},
            },
        )

    assert response.status_code == 200
    events = _events(response)
    assert not any(event["event"] == "tool" for event in events)
    done = events[-1]["response"]
    assert done["mode"] == "propose_action"
    assert done["requires_confirmation"] is True
    assert done["action_summary"] == "该操作需要在对应管理模块核对后再执行"
    assert "我可以为这批订单发起退款" not in done["answer"]
    assert "不会直接生成、提交或执行" in done["answer"]
    assert done["advanced_view"] == "orders"


def test_workspace_clarify_is_not_overridden_by_write_keyword(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "clarify",
            "tool_name": None,
            "arguments": {},
            "response": "请提供范围，确认后我会生成订货单并执行采购。",
            "reason": "需要补充采购范围",
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-action-guard-001",
                "message": "把需要补货的商品生成订货单，确认后执行采购。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    done = events[-1]["response"]
    assert done["mode"] == "clarify"
    assert done["requires_confirmation"] is False


def test_workspace_can_generate_a_review_only_copy_draft(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    called = []
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "generate_marketing_copy_draft",
            "arguments": {
                "store_id": "store-001",
                "product_name": "轻量保温杯",
                "selling_points": ["轻量", "保温 6 小时"],
                "styles": ["concise"],
                "variants_per_style": 1,
                "length": "short",
            },
            "reason": "生成待复核的商品文案",
        },
    )

    def generate_copy(tenant_id, payload):
        called.append((tenant_id, payload.product_name))
        return {
            "batch_size": 1,
            "variants": [
                {
                    "style": "concise",
                    "body": "轻装出门，也能保温 6 小时。",
                    "needs_review": True,
                }
            ],
            "publication_allowed": False,
            "action_boundary": "仅生成候选文案；发布前必须人工审核。",
        }

    monkeypatch.setattr(service.operations.ops_assistant, "generate_copy", generate_copy)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["草稿已生成，", "需要人工复核，尚未发布。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-copy-001",
                "message": "给轻量保温杯生成一条简洁短文案",
                "history": [],
                "context": {"store_id": "store-001"},
            },
        )
        audit = client.get(
            "/v1/admin/audit?event_type=ops.copywriting.generated",
            headers=ADMIN_HEADERS,
        )

    events = _events(response)
    assert response.status_code == 200
    assert called == [("tenant-test", "轻量保温杯")]
    assert any(
        event["event"] == "tool"
        and event["tool_name"] == "generate_marketing_copy_draft"
        for event in events
    )
    assert events[-1]["response"]["requires_confirmation"] is False
    assert "尚未发布" in events[-1]["response"]["answer"]
    assert audit.status_code == 200
    assert audit.json()[0]["detail"]["source"] == "workspace_agent"


def test_workspace_operations_report_suppresses_narrative_and_draft_wrapper(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    captured_report_calls = []
    response_messages = []
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_operations_assistant_report",
            "arguments": {},
            "reason": "查看经营数据摘要",
        },
    )

    def analysis_report(tenant_id, query, *, include_narrative=True):
        captured_report_calls.append(include_narrative)
        return {
            "summary": ["本期销售额 51200 元。", "订单后半段下降 52.9%。"],
            "narrative": "这是一整段不应重复展示的经营分析原文。",
            "findings": [{"code": "sales_declining"}],
        }

    def stream_generate(messages):
        response_messages.extend(messages)
        return iter(["销售额为 51200 元，订单后半段下降 52.9%，建议检查转化环节。"])

    monkeypatch.setattr(
        service.operations.ops_assistant, "analysis_report", analysis_report
    )
    monkeypatch.setattr(service.model, "stream_generate", stream_generate)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-ops-summary-only-001",
                "message": "分析一下最近的经营数据。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    done = events[-1]["response"]
    assert captured_report_calls == [False]
    prompt_payload = json.loads(response_messages[-1]["content"])
    assert prompt_payload["包含营销文案草稿"] is False
    assert "经营分析原文" not in response_messages[-1]["content"]
    assert "另有一段已核实信息" not in done["answer"]
    assert "逐字保留如下" not in done["answer"]


def test_workspace_can_answer_without_forcing_a_tool(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "answer",
            "tool_name": None,
            "arguments": None,
            "response": "可以。你可以先告诉我想关注库存、订单还是客服。",
            "reason": "这个问题不需要读取实时业务数据",
        },
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: (_ for _ in ()).throw(AssertionError("不应调用事实整理模型")),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-direct-answer-001",
                "message": "你能帮我做什么？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert not any(event["event"] == "tool" for event in events)
    done = events[-1]["response"]
    assert done["mode"] == "answer"
    assert done["tools_used"] == []
    assert done["decision_steps"] == 1


def test_workspace_replans_after_observation_and_can_use_multiple_tools(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_workspace_overview",
                "arguments": {},
                "reason": "先核对整机当前状态",
            },
            {
                "mode": "observe",
                "tool_name": "get_inventory_risk",
                "arguments": {},
                "reason": "还需要直接核对库存风险",
            },
            {
                "mode": "answer",
                "tool_name": None,
                "arguments": {},
                "response": "已取得回答所需的事实。",
                "reason": "现有证据已经足够",
            },
        ]
    )
    planning_payloads = []

    def generate_json(messages, **kwargs):
        planning_payloads.append(json.loads(messages[-1]["content"]))
        return next(decisions)

    monkeypatch.setattr(service.model, "generate_json", generate_json)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["已经分别核对整机状态和库存风险。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-replan-001",
                "message": "先看看系统状态，再确认库存有没有风险。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    tool_events = [event for event in events if event["event"] == "tool"]
    assert [event["tool_name"] for event in tool_events] == [
        "get_workspace_overview",
        "get_inventory_risk",
    ]
    assert planning_payloads[0]["verified_observations"] == []
    assert len(planning_payloads[1]["verified_observations"]) == 1
    assert len(planning_payloads[2]["verified_observations"]) == 2
    done = events[-1]["response"]
    assert [item["tool_name"] for item in done["tools_used"]] == [
        "get_workspace_overview",
        "get_inventory_risk",
    ]
    assert done["decision_steps"] == 3
    assert done["limit_reached"] is False


def test_workspace_does_not_repeat_identical_tool_query(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    calls = []
    original_execute = app.state.workspace_agent._execute
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_workspace_overview",
            "arguments": {},
            "reason": "核对整机状态",
        },
    )

    def execute_once(*args, **kwargs):
        calls.append(1)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(app.state.workspace_agent, "_execute", execute_once)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["已按本轮取得的事实整理结论。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-duplicate-001",
                "message": "现在系统怎么样？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert len(calls) == 1
    assert len([event for event in events if event["event"] == "tool"]) == 1
    assert events[-1]["response"]["limit_reached"] is True


def test_workspace_enforces_tool_step_limit_without_losing_observations(
    tmp_path, monkeypatch
) -> None:
    settings = replace(make_settings(tmp_path), max_react_steps=2)
    app = create_app(settings)
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_workspace_overview",
                "arguments": {},
                "reason": "核对整机状态",
            },
            {
                "mode": "observe",
                "tool_name": "get_channel_status",
                "arguments": {},
                "reason": "继续核对渠道状态",
            },
            {
                "mode": "observe",
                "tool_name": "get_governance_status",
                "arguments": {},
                "reason": "继续核对治理状态",
            },
        ]
    )
    monkeypatch.setattr(
        service.model, "generate_json", lambda messages, **kwargs: next(decisions)
    )
    captured = []

    def stream_generate(messages):
        captured.extend(messages)
        return iter(["已根据本轮核实到的信息整理结论，并保留未核实边界。"])

    monkeypatch.setattr(service.model, "stream_generate", stream_generate)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-tool-limit-001",
                "message": "把所有模块都检查一遍。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    tool_events = [event for event in events if event["event"] == "tool"]
    assert [event["tool_name"] for event in tool_events] == [
        "get_workspace_overview",
        "get_channel_status",
    ]
    done = events[-1]["response"]
    assert done["limit_reached"] is True
    assert done["decision_steps"] == 3
    response_payload = json.loads(captured[-1]["content"])
    assert len(response_payload["已核实结果"]) == 2
    assert "查询步数上限" in response_payload["执行边界"][-1]["message"]


def test_inventory_risk_catalog_allows_authorized_full_scope_query(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    catalog = {
        item["name"]: item for item in app.state.workspace_agent.tool_catalog()
    }
    inventory = catalog["get_inventory_risk"]
    assert "sku_id" not in inventory["input_schema"].get("required", [])
    assert "省略时查询全部" in inventory["description"]


def test_workspace_prompt_does_not_hard_code_overview_as_default() -> None:
    from ecommerce_agent.workspace_agent import (
        WORKSPACE_ACTION_REVIEW_PROMPT,
        WORKSPACE_RESPONSE_PROMPT,
        WORKSPACE_SYSTEM_PROMPT,
    )

    assert "优先使用 get_workspace_overview" not in WORKSPACE_SYSTEM_PROMPT
    assert "整机概览只适用于真正询问" in WORKSPACE_SYSTEM_PROMPT
    assert "工具不是固定流程" in WORKSPACE_SYSTEM_PROMPT
    assert "不得根据标识符的字面形式猜测" in WORKSPACE_RESPONSE_PROMPT
    assert "经营分析、指标、趋势、诊断和建议都不是文案草稿" in WORKSPACE_RESPONSE_PROMPT
    assert "即使句子里出现补货、退款、预算、发布等业务名词" in WORKSPACE_SYSTEM_PROMPT
    assert "最近对话" in WORKSPACE_SYSTEM_PROMPT
    assert "不可信业务数据" in WORKSPACE_SYSTEM_PROMPT
    assert "询问“有没有、哪些、是否、多少、为什么、风险、建议、情况”" in WORKSPACE_ACTION_REVIEW_PROMPT
    assert "不得硬编码固定工具" in WORKSPACE_ACTION_REVIEW_PROMPT
    assert "不可信业务数据" in WORKSPACE_RESPONSE_PROMPT


def test_workspace_reviews_false_action_mode_for_read_only_inventory_question(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "propose_action",
                "response": "请提供范围，确认后处理补货。",
                "reason": "涉及补货",
                "action_summary": "处理补货",
            },
            {
                "mode": "observe",
                "tool_name": "get_inventory_risk",
                "arguments": {},
                "reason": "这是在询问当前补货风险，需要查询库存事实",
            },
            {
                "mode": "answer",
                "response": "现有证据已经足够。",
                "reason": "已核实全部授权范围内的库存风险",
            },
        ]
    )
    planning_system_prompts = []

    def generate_json(messages, **kwargs):
        planning_system_prompts.append(messages[0]["content"])
        return next(decisions)

    monkeypatch.setattr(service.model, "generate_json", generate_json)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["已检查全部授权库存，当前没有需要补货的记录。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-action-review-001",
                "message": "有没有要补货的内容？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    done = events[-1]["response"]
    assert [event["tool_name"] for event in events if event["event"] == "tool"] == [
        "get_inventory_risk"
    ]
    assert done["mode"] == "answer"
    assert done["requires_confirmation"] is False
    assert done["advanced_view"] == "commerce"
    assert "确认后处理补货" not in done["answer"]
    assert "动作意图复核器" in planning_system_prompts[1]


def test_workspace_replans_with_corrected_arguments_after_query_rejection(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_customer_service_status",
                "arguments": {"scope": "wrong"},
                "reason": "查看客服情况",
            },
            {
                "mode": "observe",
                "tool_name": "get_customer_service_status",
                "arguments": {"scope": "operational"},
                "reason": "修正为真实运营范围后重新查询",
            },
            {
                "mode": "answer",
                "response": "已经取得可靠事实。",
                "reason": "客服事实已经足够",
            },
        ]
    )
    planning_payloads = []

    def generate_json(messages, **kwargs):
        planning_payloads.append(json.loads(messages[-1]["content"]))
        return next(decisions)

    monkeypatch.setattr(service.model, "generate_json", generate_json)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["已按真实运营范围核实客服情况。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-query-correction-001",
                "message": "看看当前客服接待情况。",
                "history": [],
                "context": {},
            },
        )

    tool_events = [event for event in _events(response) if event["event"] == "tool"]
    assert [event["status"] for event in tool_events] == ["rejected", "success"]
    assert "当前模块无法返回可靠结果" in tool_events[0]["summary"]
    assert planning_payloads[1]["execution_notes"][0]["type"] == "query_rejected"
    assert _events(response)[-1]["response"]["limit_reached"] is False


def test_workspace_stops_repeating_the_same_rejected_query(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    calls = []

    def same_invalid_plan(messages, **kwargs):
        calls.append(1)
        return {
            "mode": "observe",
            "tool_name": "get_order_facts",
            "arguments": {},
            "reason": "查询订单",
        }

    monkeypatch.setattr(service.model, "generate_json", same_invalid_plan)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: (_ for _ in ()).throw(AssertionError("没有事实时不应生成总结")),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-rejected-loop-001",
                "message": "查一下订单。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    rejected = [event for event in events if event.get("status") == "rejected"]
    done = events[-1]["response"]
    assert len(calls) == 2
    assert len(rejected) == 1
    assert done["mode"] == "clarify"
    assert done["limit_reached"] is True
    assert "店铺编号" in done["answer"]
    assert "订单编号" in done["answer"]


def test_workspace_final_composition_receives_recent_dialogue_as_untrusted_context(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_inventory_risk",
                "arguments": {"sku_id": "SKU-001"},
                "reason": "结合上轮商品继续查看库存",
            },
            {
                "mode": "answer",
                "response": "事实已经足够。",
                "reason": "已核实该商品库存",
            },
        ]
    )
    response_messages = []
    monkeypatch.setattr(
        service.model, "generate_json", lambda messages, **kwargs: next(decisions)
    )

    def stream_generate(messages):
        response_messages.extend(messages)
        return iter(["SKU-001 当前没有补货风险。"])

    monkeypatch.setattr(service.model, "stream_generate", stream_generate)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-history-compose-001",
                "message": "那它的库存呢？",
                "history": [
                    {"role": "user", "content": "查一下 SKU-001。"},
                    {"role": "assistant", "content": "已找到商品 SKU-001。"},
                ],
                "context": {},
            },
        )

    assert response.status_code == 200
    payload = json.loads(response_messages[-1]["content"])
    assert payload["最近对话"] == [
        {"角色": "店主", "内容": "查一下 SKU-001。"},
        {"角色": "统筹助手", "内容": "已找到商品 SKU-001。"},
    ]
    assert "不可信业务数据" in response_messages[0]["content"]


def test_workspace_catalog_and_order_lists_cover_broad_management_questions(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    catalog = {
        item["name"]: item for item in app.state.workspace_agent.tool_catalog()
    }
    assert catalog["get_catalog_status"]["input_schema"].get("required", []) == []
    assert catalog["get_order_management_status"]["input_schema"].get("required", []) == []

    catalog_calls = []
    order_calls = []
    monkeypatch.setattr(
        service.operations.catalog,
        "list_items",
        lambda tenant_id, **kwargs: catalog_calls.append((tenant_id, kwargs)) or [],
    )
    monkeypatch.setattr(
        service.operations.orders,
        "list_orders",
        lambda tenant_id, **kwargs: order_calls.append((tenant_id, kwargs)) or [],
    )
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_catalog_status",
                "arguments": {"status": "active"},
                "reason": "先查看当前在售商品",
            },
            {
                "mode": "observe",
                "tool_name": "get_order_management_status",
                "arguments": {"order_status": "paid"},
                "reason": "再查看待履约订单",
            },
            {
                "mode": "answer",
                "response": "查询已经完成。",
                "reason": "两类事实已经核实",
            },
        ]
    )
    monkeypatch.setattr(
        service.model, "generate_json", lambda messages, **kwargs: next(decisions)
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["当前没有在售商品，也没有待履约订单。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-broad-list-001",
                "message": "目前有哪些在售商品和待处理订单？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert [event["tool_name"] for event in events if event["event"] == "tool"] == [
        "get_catalog_status",
        "get_order_management_status",
    ]
    assert catalog_calls == [
        (
            "tenant-test",
            {"store_id": None, "status": "active", "limit": 20},
        )
    ]
    assert order_calls == [
        (
            "tenant-test",
            {
                "store_id": None,
                "order_status": "paid",
                "limit": 20,
                "service_scope": "operational",
            },
        )
    ]
    assert events[-1]["response"]["requires_confirmation"] is False


def test_workspace_preserves_verified_facts_when_later_planning_is_invalid(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_inventory_risk",
                "arguments": {},
                "reason": "查看库存风险",
            },
            {"mode": "not-a-valid-mode"},
        ]
    )
    monkeypatch.setattr(
        service.model, "generate_json", lambda messages, **kwargs: next(decisions)
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["已根据成功取得的库存事实整理结果。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-planning-fallback-001",
                "message": "检查库存后给我结论。",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert any(event.get("stage") == "planning_fallback" for event in events)
    assert not any(event["event"] == "error" for event in events)
    done = events[-1]["response"]
    assert done["answer"] == "已根据成功取得的库存事实整理结果。"
    assert done["degraded"] is True
    assert done["degraded_reasons"] == ["planning_output_invalid"]


def test_workspace_uses_verified_product_language_when_response_model_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_inventory_risk",
                "arguments": {},
                "reason": "查看库存风险",
            },
            {
                "mode": "answer",
                "response": "事实已经足够。",
                "reason": "库存事实已核实",
            },
        ]
    )
    monkeypatch.setattr(
        service.model, "generate_json", lambda messages, **kwargs: next(decisions)
    )

    def unavailable_stream(messages):
        raise ModelUnavailableError("provider unavailable")
        yield ""

    monkeypatch.setattr(service.model, "stream_generate", unavailable_stream)

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-response-fallback-001",
                "message": "有没有库存风险？",
                "history": [],
                "context": {},
            },
        )

    events = _events(response)
    assert not any(event["event"] == "error" for event in events)
    done = events[-1]["response"]
    assert done["answer"].startswith("已完成事实核对：")
    assert "当前查询范围内暂无数据" in done["answer"]
    assert done["degraded"] is True
    assert done["degraded_reasons"] == ["response_model_unavailable"]


def test_workspace_first_round_explicit_write_request_keeps_propose_action(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent

    def plan_once(_messages, **_kwargs):
        return {
            "mode": "propose_action",
            "response": "确认要对这些订单执行退款？",
            "action_summary": "执行退款",
            "reason": "用户明确要求退款",
        }

    monkeypatch.setattr(service.model, "generate_json", plan_once)
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-write-round1-001",
                "message": "把这些订单全部退款",
                "history": [],
                "context": {},
            },
        )

    assert response.status_code == 200
    events = _events(response)
    done = events[-1]["response"]
    assert done["mode"] == "propose_action"
    assert done["requires_confirmation"] is True
    assert done["tools_used"] == []
    assert done["answer"]


def test_workspace_dependent_task_does_not_break_strict_tool_schema(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    service.operations.inventory.upsert(
        "tenant-test",
        InventoryBalanceUpsert(
            connector_id="dependent-fixture",
            store_id="qingchuan-flagship-001",
            warehouse_id="warehouse-001",
            sku_id="QC-DEPENDENT-01",
            on_hand="8",
            average_daily_sales="2",
            source_updated_at=datetime.now(UTC) - timedelta(hours=1),
            source_id="inventory-dependent-001",
        ),
    )

    def plan_once(_messages, **_kwargs):
        return {
            "tasks": [
                {
                    "task_id": "inventory",
                    "objective": "核对库存风险",
                    "tool_name": "get_inventory_risk",
                    "arguments": {"store_id": "qingchuan-flagship-001"},
                    "depends_on": [],
                },
                {
                    "task_id": "catalog",
                    "objective": "按前置事实核对商品目录",
                    "tool_name": "get_catalog_status",
                    "arguments": {
                        "store_id": "qingchuan-flagship-001",
                        "status": "active",
                    },
                    "depends_on": ["inventory"],
                },
            ]
        }

    monkeypatch.setattr(service.model, "generate_json", plan_once)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["已核对库存与商品目录。"]),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-dependent-strict-001",
                "message": "查看库存风险并按库存核对商品目录。",
                "history": [],
                "context": {"store_id": "qingchuan-flagship-001"},
            },
        )

    assert response.status_code == 200
    events = _events(response)
    assert all(event.get("event") != "error" for event in events)
    done = events[-1]["response"]
    assert done["completion_status"] in {"completed", "partial"}
    statuses = [item["status"] for item in done["task_results"]]
    assert statuses[0] in {"success", "no_data"}
    assert statuses[1] in {"success", "no_data"}
