from __future__ import annotations

import json

from ecommerce_agent.workspace_presenter import (
    TOOL_LABELS,
    critical_fact_values,
    observation_data_status,
    observation_summary,
    present_observation,
)


def test_metric_presenter_distinguishes_no_data_from_verified_zero() -> None:
    no_data = {
        "display_name": "已支付且未取消订单金额",
        "value": "0.00",
        "unit": "currency",
        "quality": "no_data",
        "evidence_count": 0,
    }
    verified_zero = {
        "display_name": "已支付且未取消订单金额",
        "value": "0.00",
        "unit": "currency",
        "quality": "available",
        "evidence_count": 1,
    }

    no_data_view = present_observation("get_business_metric", no_data)
    zero_view = present_observation("get_business_metric", verified_zero)

    assert observation_data_status("get_business_metric", no_data) == "no_data"
    assert "暂无数据" in no_data_view["已核实信息"][0]
    assert critical_fact_values(no_data_view) == []
    assert observation_data_status("get_business_metric", verified_zero) == "success"
    assert "0.00 元" in zero_view["已核实信息"][0]
    assert critical_fact_values(zero_view) == ["0.00", "1"]


def test_inventory_presenter_distinguishes_empty_scope_from_zero_replenishment() -> None:
    empty = {"risks": []}
    verified = {
        "risks": [
            {
                "sku_id": "SKU-HEALTHY-001",
                "risk_code": "healthy",
                "risk_level": "low",
                "available": "100.00",
                "coverage_days": "100.00",
                "recommended_replenishment": "0.00",
            }
        ]
    }

    empty_view = present_observation("get_inventory_risk", empty)
    verified_view = present_observation("get_inventory_risk", verified)

    assert observation_data_status("get_inventory_risk", empty) == "no_data"
    assert "暂无数据" in empty_view["已核实信息"][0]
    assert observation_data_status("get_inventory_risk", verified) == "success"
    assert "建议补货 0.00 件" in json.dumps(verified_view, ensure_ascii=False)
    assert {"1", "0", "SKU-HEALTHY-001", "0.00"} <= set(
        critical_fact_values(verified_view)
    )


def test_product_search_requires_unique_resolution_before_dependent_query() -> None:
    ambiguous = {
        "resolution": "ambiguous",
        "items": [{"sku_id": "SKU-A"}, {"sku_id": "SKU-B"}],
    }
    resolved = {
        "resolution": "resolved",
        "items": [{"sku_id": "SKU-A"}],
    }

    assert observation_data_status("search_products", ambiguous) == "no_data"
    assert observation_data_status("search_products", resolved) == "success"


def test_customer_service_is_expressed_as_people_and_work_not_internal_fields() -> None:
    view = present_observation(
        "get_customer_service_status",
        {
            "customer_team": {
                "total": 5,
                "online": 3,
                "working": 2,
                "available": 1,
            },
            "handoffs": {
                "total": 9,
                "open": 4,
                "unassigned": 1,
                "due_soon": 1,
                "breached": 0,
                "operators": {"active": 4, "available": 1},
            },
            "recent_conversations": [{"id": "one"}],
            "dispatch": {"alerts": {"open": 2}},
        },
    )

    rendered = json.dumps(view, ensure_ascii=False)
    assert "总共 5 位客服，在线 3 位，正在工作 2 位" in rendered
    assert "可继续接待 1 位" in rendered
    assert "人工接待任务目前待处理 4 个" in rendered
    assert "total" not in rendered
    assert "active" not in rendered
    assert "available" not in rendered
    assert "operators" not in rendered
    assert observation_summary(view).startswith("总共 5 位客服")


def test_every_workspace_tool_has_a_customer_facing_chinese_label() -> None:
    expected = {
        "get_workspace_overview",
        "get_customer_service_status",
        "get_governance_status",
        "get_channel_status",
        "get_module_registry",
        "get_catalog_status",
        "get_order_management_status",
        "get_operations_assistant_report",
        "generate_marketing_copy_draft",
        "get_product_facts",
        "search_products",
        "get_order_facts",
        "get_inventory_risk",
        "get_business_metric",
        "get_competitor_price_analysis",
        "get_competitive_intelligence",
        "get_marketing_diagnosis",
        "get_profit_reconciliation",
        "get_listing_traffic_insights",
    }
    assert expected <= set(TOOL_LABELS)
    assert all("_" not in label for label in TOOL_LABELS.values())
    for tool_name in TOOL_LABELS:
        rendered = json.dumps(
            present_observation(
                tool_name,
                {
                    "total": 99,
                    "active": 88,
                    "available": 77,
                    "internal_debug_field": "must-not-leak",
                },
            ),
            ensure_ascii=False,
        )
        assert "internal_debug_field" not in rendered
        assert "must-not-leak" not in rendered
        assert '"total"' not in rendered
        assert '"active"' not in rendered
        assert '"available"' not in rendered


def test_common_business_results_are_translated_before_reaching_answer_model() -> None:
    samples = {
        "get_business_metric": {
            "display_name": "售后订单占比",
            "value": "0.1250",
            "unit": "ratio",
            "evidence_count": 8,
            "definition_version": "1.0",
        },
        "get_inventory_risk": {
            "risks": [
                {
                    "sku_id": "SKU-1",
                    "risk_code": "stockout_risk",
                    "risk_level": "high",
                    "available": "3.00",
                    "coverage_days": "2.00",
                    "recommended_replenishment": "20.00",
                }
            ]
        },
        "get_profit_reconciliation": {
            "profit": {
                "currency": "CNY",
                "gross_sales": "100.00",
                "approved_refunds": "10.00",
                "expense_total": "30.00",
                "management_profit": "60.00",
            },
            "reconciliation_tasks": [],
        },
    }

    metric = json.dumps(
        present_observation("get_business_metric", samples["get_business_metric"]),
        ensure_ascii=False,
    )
    assert "12.50%" in metric
    assert "evidence_count" not in metric

    inventory = json.dumps(
        present_observation("get_inventory_risk", samples["get_inventory_risk"]),
        ensure_ascii=False,
    )
    assert "临近缺货" in inventory
    assert "risk_code" not in inventory

    finance = json.dumps(
        present_observation(
            "get_profit_reconciliation", samples["get_profit_reconciliation"]
        ),
        ensure_ascii=False,
    )
    assert "预计利润 60.00 CNY" in finance
    assert "management_profit" not in finance


def test_forecast_and_plan_presenters_hide_internal_status_values() -> None:
    forecast = present_observation(
        "get_demand_forecast",
        {
            "forecast": {
                "status": "degraded",
                "champion_model": "last_value",
            },
            "freshness": {"status": "stale"},
        },
    )
    plan = present_observation(
        "get_inventory_plan",
        {
            "inventory_plan": {
                "risk_level": "medium",
                "recommended_order_qty": "0",
                "action_mode": "advisory_only",
            }
        },
    )

    forecast_text = json.dumps(forecast, ensure_ascii=False)
    plan_text = json.dumps(plan, ensure_ascii=False)
    assert "已降级" in forecast_text
    assert "最近值模型" in forecast_text
    assert "已过期" in forecast_text
    assert "degraded" not in forecast_text
    assert "last_value" not in forecast_text
    assert "stale" not in forecast_text
    assert "仅建议" in plan_text
    assert "advisory_only" not in plan_text


def test_operations_report_does_not_forward_long_model_narrative() -> None:
    view = present_observation(
        "get_operations_assistant_report",
        {
            "summary": [
                "本期销售额 51200 元。",
                "后半段订单较前半段下降 52.9%。",
            ],
            "narrative": "这是一整段不应在统筹对话里重复展示的模型分析原文。",
            "findings": [{"code": "sales_declining"}],
        },
    )

    rendered = json.dumps(view, ensure_ascii=False)
    assert "本期销售额 51200 元" in rendered
    assert "订单较前半段下降 52.9%" in rendered
    assert "模型分析原文" not in rendered
    assert "分析中发现 1 个值得关注的经营信号" in rendered
