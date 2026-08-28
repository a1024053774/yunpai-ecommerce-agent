from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import CatalogItemUpsert, OrderUpsert
from ecommerce_agent.business.inventory import InventoryBalanceUpsert
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.llm import ModelError, ModelUnavailableError
from ecommerce_agent.workspace_agent import WorkspaceAgent
from ecommerce_agent.workspace_presenter import (
    answer_preserves_critical_values,
    present_observation,
)
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
    assert "【经营指标】" in done["answer"]
    assert "暂时无法判断" in done["answer"]


def test_workspace_no_data_is_a_completed_verified_result(tmp_path, monkeypatch) -> None:
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
        lambda _messages: iter(["最近收入暂无数据。"]),
    )

    session_id = "workspace:test-no-data-completed-001"
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": session_id,
                "message": "查看最近收入。",
                "history": [],
                "context": {},
            },
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{session_id}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    done = _events(response)[-1]["response"]
    assert done["completion_status"] == "completed"
    assert done["delivery_mode"] == "verified_final"
    assert done["task_results"][0]["status"] == "no_data"
    assert done["task_results"][0]["error_summary"] is None
    assert messages[-1]["status"] == "completed"


def test_workspace_success_and_no_data_are_completed_together(tmp_path, monkeypatch) -> None:
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
                    "objective": "核对库存",
                    "tool_name": "get_inventory_risk",
                    "arguments": {},
                    "depends_on": [],
                },
                {
                    "task_id": "revenue",
                    "objective": "核对收入",
                    "tool_name": "get_business_metric",
                    "arguments": {"metric": "gross_revenue"},
                    "depends_on": [],
                },
            ]
        },
    )

    def run_task(task, *_args):
        if task.task_id == "revenue":
            return WorkspaceTaskResult(
                task_id=task.task_id,
                objective=task.objective,
                tool_name=task.tool_name,
                tool_label="经营指标",
                status="no_data",
                verified_facts=["当前查询范围内暂无数据。"],
            )
        return WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="库存风险",
            status="success",
            verified_facts=["共检查 10 个库存记录。"],
        )

    monkeypatch.setattr(workspace, "_run_read_task", run_task)
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["库存有 10 条记录。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-success-no-data-001",
                "message": "查看库存和收入。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert done["completion_status"] == "completed"
    assert done["delivery_mode"] == "verified_final"
    assert "暂无数据" in done["answer"]
    assert "critical_value_mismatch" in done["degraded_reasons"]
    assert [item["status"] for item in done["task_results"]] == [
        "success",
        "no_data",
    ]


def test_workspace_read_plan_preserves_full_tool_result_before_presentation(
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
                    "task_id": "modules",
                    "objective": "核对业务模块",
                    "tool_name": "get_module_registry",
                    "arguments": {},
                    "depends_on": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        workspace,
        "_execute",
        lambda *_args, **_kwargs: {
            "modules": [
                {
                    "display_name": f"业务能力 {chr(65 + index)}",
                    "status": "available",
                }
                for index in range(20)
            ]
        },
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["共登记 20 项业务能力，其中 20 项当前可用。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-uncompressed-result-001",
                "message": "查看全部业务模块。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert "20 项业务能力" in done["answer"]
    assert "13 项业务能力" not in done["answer"]
    assert done["delivery_mode"] == "verified_final"


def test_workspace_deterministic_answer_preserves_every_verified_fact() -> None:
    forecast_view = present_observation(
        "get_demand_forecast",
        {
            "forecast": {
                "sku_id": "YP-SKU-001",
                "status": "degraded",
                "champion_model": "last_value",
                "points": [{"p50": "3", "p80": "4", "p95": "5"}],
            },
            "freshness": {"status": "stale"},
        },
    )
    plan_view = present_observation(
        "get_inventory_plan",
        {
            "inventory_plan": {
                "sku_id": "YP-SKU-001",
                "risk_level": "medium",
                "recommended_order_qty": "0",
                "plan_quality": "valid",
                "action_mode": "advisory_only",
                "freshness": {"status": "stale"},
            }
        },
    )
    observations = [
        {
            "status": "success",
            "objective": "核对需求预测",
            "tool_label": "需求预测",
            "result": forecast_view,
        },
        {
            "status": "success",
            "objective": "核对库存计划",
            "tool_label": "库存计划",
            "result": plan_view,
        },
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "P50 为 3，P80 为 4，P95 为 5" in answer
    assert "库存计划质量为有效" in answer
    assert "库存计划证据新鲜度为已过期" in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


def test_workspace_deterministic_answer_does_not_treat_model_objective_as_fact() -> None:
    view = present_observation(
        "list_recommendations",
        {
            "items": [
                {
                    "recommendation_id": "sim-rec-001",
                    "recommendation_type": "保持观察",
                    "state": "draft",
                    "degraded": False,
                }
            ]
        },
    )
    observations = [
        {
            "status": "success",
            "objective": "查询当前状态、建议类型和证据状态摘要",
            "tool_name": "list_recommendations",
            "tool_label": "Business information",
            "result": view,
        }
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "【商品经营建议】" in answer
    assert "查询当前状态、建议类型和证据状态摘要" not in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


def test_workspace_deterministic_answer_distinguishes_inventory_warehouses() -> None:
    view = present_observation(
        "get_inventory_risk",
        {
            "risks": [
                {
                    "sku_id": "SKU-001",
                    "warehouse_id": "QC-WH-HZ",
                    "risk_code": "stockout_risk",
                    "risk_level": "high",
                    "available": "4.00",
                    "coverage_days": "0.80",
                    "recommended_replenishment": "126.00",
                },
                {
                    "sku_id": "SKU-001",
                    "warehouse_id": "QC-WH-WH",
                    "risk_code": "replenishment_due",
                    "risk_level": "medium",
                    "available": "15.00",
                    "coverage_days": "7.50",
                    "recommended_replenishment": "45.00",
                },
            ]
        },
    )
    observations = [
        {
            "status": "success",
            "tool_name": "get_inventory_risk",
            "tool_label": "库存风险",
            "result": view,
        }
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "仓库 QC-WH-HZ" in answer
    assert "仓库 QC-WH-WH" in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


def test_workspace_deterministic_answer_preserves_order_summary_and_logistics() -> None:
    view = present_observation(
        "get_order_facts",
        {
            "orders": [
                {
                    "order_id": "QC-ORDER-1001",
                    "order_status": "shipped",
                    "payment_status": "paid",
                    "total_amount": "499.00",
                    "currency": "CNY",
                    "lines": [{}],
                    "logistics": {
                        "carrier": "圆通",
                        "status": "in_transit",
                        "last_event": "已到达南京转运中心",
                    },
                }
            ]
        },
    )
    observations = [
        {
            "status": "success",
            "tool_name": "get_order_facts",
            "tool_label": "订单与物流信息",
            "result": view,
        }
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "共找到 1 个订单" in answer
    assert "当前运输中" in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


def test_workspace_deterministic_answer_preserves_marketing_and_profit_fields() -> None:
    marketing_view = present_observation(
        "get_marketing_diagnosis",
        {
            "totals": {
                "spend": "275.00",
                "attributed_orders": 2,
                "attributed_revenue": "720.00",
                "roas": "2.62",
                "ctr": "0.015",
            },
            "findings": [
                {"recommendation": "停止或调整投放前需人工审批"},
                {"recommendation": "建议生成内容草稿并进行人工事实审核"},
            ],
        },
    )
    finance_view = present_observation(
        "get_profit_reconciliation",
        {
            "profit": {
                "currency": "CNY",
                "gross_sales": "4181.00",
                "approved_refunds": "30.00",
                "expense_total": "2660.00",
                "management_profit": "1491.00",
            },
            "reconciliation_tasks": [{}],
        },
    )
    observations = [
        {
            "status": "success",
            "tool_name": "get_marketing_diagnosis",
            "tool_label": "营销投放诊断",
            "result": marketing_view,
        },
        {
            "status": "success",
            "tool_name": "get_profit_reconciliation",
            "tool_label": "利润与结算核对",
            "result": finance_view,
        },
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "带来 2 个归因订单" in answer
    assert "预计利润 1491.00 CNY" in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


def test_workspace_deterministic_answer_preserves_traffic_evidence() -> None:
    view = present_observation(
        "get_listing_traffic_insights",
        {
            "sku_id": "YP-SKU-TRAFFIC-001",
            "insights": [
                {
                    "experiment": {
                        "experiment_id": "exp-traffic-001",
                        "status": "completed",
                        "primary_metric": "ctr",
                    },
                    "analysis": {
                        "evidence": {
                            "effect": {
                                "absolute": "0.015",
                                "direction": "positive",
                            },
                            "confidence_interval": {
                                "low": "0.005",
                                "high": "0.025",
                            },
                            "quality_gate": {"status": "passed"},
                            "statistical_conclusion": "positive_effect",
                        }
                    },
                    "freshness": {"status": "current"},
                }
            ],
            "freshness": {"status": "current"},
        },
    )
    observations = [
        {
            "status": "success",
            "tool_name": "get_listing_traffic_insights",
            "tool_label": "流量实验洞察",
            "result": view,
        }
    ]

    answer = WorkspaceAgent._deterministic_answer(observations, [])

    assert "质量门禁为通过" in answer
    assert "统计结论为正向变化" in answer
    assert "可信区间下限为 0.005，上限为 0.025" in answer
    assert answer_preserves_critical_values(answer, observations, require_all=True)


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


def test_workspace_single_observe_rejects_answer_that_changes_verified_count(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_module_registry",
                "arguments": {},
                "reason": "核对业务模块",
            },
            {
                "mode": "answer",
                "response": "模块信息已核实。",
                "reason": "业务模块已核实",
            },
        ]
    )
    monkeypatch.setattr(
        service.model, "generate_json", lambda _messages, **_kwargs: next(decisions)
    )
    monkeypatch.setattr(
        workspace,
        "_execute",
        lambda *_args, **_kwargs: {
            "modules": [
                {"display_name": "商品管理", "status": "available"}
                for _ in range(13)
            ]
        },
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["当前共登记 13 项业务能力，其中 12 项当前可用。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-single-observe-number-guard-001",
                "message": "查看当前业务模块。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert "12 项" not in done["answer"]
    assert "13 项" in done["answer"]
    assert "critical_value_mismatch" in done["degraded_reasons"]


def test_workspace_composite_rejects_answer_that_adds_conflicting_verified_count(
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
                    "task_id": "modules",
                    "objective": "核对业务模块",
                    "tool_name": "get_module_registry",
                    "arguments": {},
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
            tool_label="业务能力情况",
            status="success",
            verified_facts=["共登记 13 项业务能力，其中 13 项当前可用。"],
            critical_values=["13"],
            structured_data={},
        ),
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["当前共登记 13 项业务能力，其中 12 项当前可用。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-composite-conflicting-count-001",
                "message": "查看当前业务模块。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert "12 项" not in done["answer"]
    assert "13 项" in done["answer"]
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
        assert "/v1/admin/workspace/conversations" in workspace.text
        assert "/chat/stream" in workspace.text
        assert "今天想把什么交给我" in workspace.text
        assert "今日态势" in workspace.text
        assert "需要你关注" in workspace.text
        assert "查看处理过程" in workspace.text
        assert "查看执行轨迹" not in workspace.text
        assert "追踪：</strong>" not in workspace.text
        assert "result.tool_name" not in workspace.text
        assert "/admin/advanced" in workspace.text
        assert 'id="workspaceImageInput"' in workspace.text
        assert 'id="clearWorkspaceImageButton"' in workspace.text
        assert "clipboardData" in workspace.text
        assert "不需要上传即可驱动图片观察" in workspace.text
        assert "messageObjectUrls" in workspace.text
        assert "URL.revokeObjectURL" in workspace.text
        assert "await loadCapabilities();" in workspace.text

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
    meta = next(event for event in events if event["event"] == "meta")
    assert meta["delivery_mode"] == "pending"
    answer_payload = response_messages[-1]["content"]
    assert '"已核实结果"' in answer_payload
    assert '"verified_result"' not in answer_payload
    assert '"readiness"' not in answer_payload
    assert '"operators"' not in answer_payload


def test_workspace_single_observe_reports_no_data_in_the_tool_event(
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
                "response": "库存风险暂无数据。",
                "reason": "库存事实已核实",
            },
        ]
    )
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: next(decisions),
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["库存风险暂无数据。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-single-no-data-status-001",
                "message": "查看库存风险。",
                "history": [],
                "context": {},
            },
        )

    tool_event = next(event for event in _events(response) if event["event"] == "tool")
    assert tool_event["status"] == "no_data"


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


def test_workspace_persists_fallback_reason_in_processing_history(tmp_path, monkeypatch) -> None:
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
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "现在系统怎么样？", "context": {}},
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    processing = messages[-1]["processing"]
    assert "response_stream_failed" in processing["degraded_reasons"]


def test_workspace_persists_fact_conflict_reason_in_processing_history(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    workspace = app.state.workspace_agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "observe",
            "tool_name": "get_module_registry",
            "arguments": {},
            "reason": "核对业务模块",
        },
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda messages: iter(["当前共登记 13 项业务能力，其中 12 项当前可用。"]),
    )
    monkeypatch.setattr(
        workspace,
        "_execute",
        lambda *_args, **_kwargs: {
            "modules": [
                {"display_name": "商品管理", "status": "available"}
                for _ in range(13)
            ]
        },
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={"message": "查看当前业务模块。", "context": {}},
        )
        messages = client.get(
            f"/v1/admin/workspace/conversations/{conversation['id']}/messages",
            headers=ADMIN_HEADERS,
        ).json()

    assert response.status_code == 200
    assert "critical_value_mismatch" in messages[-1]["processing"]["degraded_reasons"]


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
    assert done["delivery_mode"] == "control_response"


def test_workspace_direct_control_response_is_not_marked_as_verified_fact(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    monkeypatch.setattr(
        app.state.agent.model,
        "generate_json",
        lambda messages, **kwargs: {
            "mode": "clarify",
            "tool_name": None,
            "arguments": {},
            "response": "请补充商品编号。",
            "missing_information": ["sku_id"],
            "reason": "需要补充商品编号",
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-control-delivery-001",
                "message": "查这个商品。",
                "history": [],
                "context": {},
            },
        )

    done = _events(response)[-1]["response"]
    assert done["delivery_mode"] == "control_response"
    assert done["completion_status"] == "completed"


def test_workspace_redacts_generated_answer_before_sse(tmp_path, monkeypatch) -> None:
    app = create_app(make_settings(tmp_path))
    decisions = iter(
        [
            {
                "mode": "observe",
                "tool_name": "get_workspace_overview",
                "arguments": {},
                "reason": "核对整机状态",
            },
            {
                "mode": "answer",
                "response": "状态已核对",
                "reason": "事实已核对",
            },
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
        lambda messages: iter(["管理员密码: secret"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-answer-redaction-001",
                "message": "查看整机状态。",
                "history": [],
                "context": {},
            },
        )

    assert "secret" not in response.text
    assert "[REDACTED]" in response.text
    assert "secret" not in _events(response)[-1]["response"]["answer"]


def test_workspace_redacts_model_text_from_the_entire_sse_event(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    monkeypatch.setattr(
        service.model,
        "generate_json",
        lambda _messages, **_kwargs: {
            "tasks": [
                {
                    "task_id": "modules",
                    "objective": "管理员密码: secret",
                    "tool_name": "get_module_registry",
                    "arguments": {},
                    "depends_on": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        service.model,
        "stream_generate",
        lambda _messages: iter(["业务能力信息已核实。"]),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-event-redaction-001",
                "message": "查看业务模块。",
                "history": [],
                "context": {},
            },
        )

    assert "secret" not in response.text
    assert "[REDACTED]" in response.text


def test_workspace_model_clarification_is_bounded_by_available_capabilities(
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
            "missing_information": ["store_id", "sku_id"],
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
    assert "请补充：店铺编号、商品编号。" in done["answer"]
    assert "我会生成" not in done["answer"]
    assert "执行采购" not in done["answer"]
    assert "不会据此直接生成、提交或执行" in done["answer"]


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
        conversation = client.post(
            "/v1/admin/workspace/conversations", headers=ADMIN_HEADERS
        ).json()
        app.state.agent.db.append_workspace_message(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation["id"],
            role="user",
            content="查一下 SKU-001。",
        )
        app.state.agent.db.append_workspace_message(
            tenant_id="tenant-test",
            admin_id="admin-test",
            conversation_id=conversation["id"],
            role="assistant",
            content="已找到商品 SKU-001。",
        )
        response = client.post(
            f"/v1/admin/workspace/conversations/{conversation['id']}/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "message": "那它的库存呢？",
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
    assert "【库存风险】当前查询范围内暂无数据" in done["answer"]
    assert done["degraded"] is True
    assert done["degraded_reasons"] == [
        "planning_output_invalid",
        "critical_value_mismatch",
    ]


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
    assert done["answer"].startswith("核实结果：")
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


def test_workspace_dependency_argument_reference_reaches_strict_tool_schema(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    source_time = datetime.now(UTC) - timedelta(hours=1)
    service.operations.catalog.upsert(
        "tenant-test",
        CatalogItemUpsert(
            connector_id="dependent-fixture",
            store_id="qingchuan-flagship-001",
            item_id="dependent-item-001",
            sku_id="QC-DEPENDENT-01",
            title="依赖参数测试商品",
            status="active",
            sale_price="99.00",
            source_updated_at=source_time,
            source_id="catalog-dependent-001",
        ),
    )
    service.operations.inventory.upsert(
        "tenant-test",
        InventoryBalanceUpsert(
            connector_id="dependent-fixture",
            store_id="qingchuan-flagship-001",
            warehouse_id="warehouse-001",
            sku_id="QC-DEPENDENT-01",
            on_hand="8",
            average_daily_sales="2",
            source_updated_at=source_time,
            source_id="inventory-dependent-001",
        ),
    )
    seen_inventory_arguments: list[dict] = []
    original_validate_selection = service.tools.validate_selection

    def capture_inventory_arguments(**kwargs):
        if kwargs["name"] == "get_inventory_risk":
            seen_inventory_arguments.append(dict(kwargs["arguments"]))
        return original_validate_selection(**kwargs)

    monkeypatch.setattr(
        service.tools, "validate_selection", capture_inventory_arguments
    )

    def plan_once(_messages, **_kwargs):
        return {
            "tasks": [
                {
                    "task_id": "catalog",
                    "objective": "查找目标商品",
                    "tool_name": "get_catalog_status",
                    "arguments": {
                        "store_id": "qingchuan-flagship-001",
                        "status": "active",
                    },
                    "depends_on": [],
                },
                {
                    "task_id": "inventory",
                    "objective": "按商品编号核对库存风险",
                    "tool_name": "get_inventory_risk",
                    "arguments": {"store_id": "qingchuan-flagship-001"},
                    "argument_refs": {
                        "sku_id": {
                            "task_id": "catalog",
                            "path": ["items", 0, "sku_id"],
                        }
                    },
                    "depends_on": ["catalog"],
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
    assert done["completion_status"] == "completed"
    assert [item["status"] for item in done["task_results"]] == [
        "success",
        "success",
    ]
    assert seen_inventory_arguments == [
        {
            "store_id": "qingchuan-flagship-001",
            "sku_id": "QC-DEPENDENT-01",
        }
    ]


def test_workspace_missing_dependency_argument_path_is_typed_failure(
    tmp_path, monkeypatch
) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent

    def plan_once(_messages, **_kwargs):
        return {
            "tasks": [
                {
                    "task_id": "catalog",
                    "objective": "查找目标商品",
                    "tool_name": "get_catalog_status",
                    "arguments": {"store_id": "empty-store-001"},
                    "depends_on": [],
                },
                {
                    "task_id": "inventory",
                    "objective": "按商品编号核对库存风险",
                    "tool_name": "get_inventory_risk",
                    "arguments": {"store_id": "empty-store-001"},
                    "argument_refs": {
                        "sku_id": {
                            "task_id": "catalog",
                            "path": ["items", 0, "sku_id"],
                        }
                    },
                    "depends_on": ["catalog"],
                },
            ]
        }

    monkeypatch.setattr(service.model, "generate_json", plan_once)
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspace/chat/stream",
            headers=ADMIN_HEADERS,
            json={
                "session_id": "workspace:test-dependent-missing-001",
                "message": "查找商品后核对该商品的库存。",
                "history": [],
                "context": {"store_id": "empty-store-001"},
            },
        )

    assert response.status_code == 200
    events = _events(response)
    assert all(event.get("event") != "error" for event in events)
    done = events[-1]["response"]
    assert [item["status"] for item in done["task_results"]] == [
        "success",
        "failed",
    ]
    assert (
        done["task_results"][1]["error_summary"]
        == "前置核实结果不足，未能继续查询。"
    )
