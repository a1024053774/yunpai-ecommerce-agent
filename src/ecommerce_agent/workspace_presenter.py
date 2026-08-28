from __future__ import annotations

import re
from decimal import Decimal
from typing import Any


TOOL_LABELS: dict[str, str] = {
    "get_workspace_overview": "经营全局概况",
    "get_customer_service_status": "客服与接待情况",
    "get_governance_status": "知识与自进化情况",
    "get_channel_status": "渠道连接情况",
    "get_module_registry": "业务能力情况",
    "get_catalog_status": "商品目录情况",
    "get_order_management_status": "近期订单情况",
    "get_operations_assistant_report": "运营分析",
    "generate_marketing_copy_draft": "营销文案草稿",
    "get_product_facts": "商品信息",
    "search_products": "商品搜索结果",
    "get_order_facts": "订单与物流信息",
    "get_inventory_risk": "库存风险",
    "get_business_metric": "经营指标",
    "get_competitor_price_analysis": "竞品价格分析",
    "get_competitive_intelligence": "竞品情报",
    "get_marketing_diagnosis": "营销投放诊断",
    "get_profit_reconciliation": "利润与结算核对",
    "get_listing_traffic_insights": "流量实验洞察",
    "get_demand_forecast": "需求预测",
    "get_inventory_plan": "库存计划",
    "list_recommendations": "商品经营建议",
    "get_recommendation_audit_trail": "建议审计记录",
}


STATUS_LABELS: dict[str, str] = {
    "active": "正常",
    "inactive": "停用",
    "available": "可用",
    "created": "已创建",
    "fulfilling": "履约中",
    "deleted": "已删除",
    "no_data": "暂无数据",
    "success": "已核实",
    "traceable": "来源可追溯",
    "source_id_missing": "来源信息不完整",
    "resolved": "已找到唯一结果",
    "ambiguous": "找到多个可能结果",
    "no_match": "未找到结果",
    "paid": "已支付",
    "unpaid": "未支付",
    "partially_refunded": "部分退款",
    "refunded": "已退款",
    "pending": "待处理",
    "processing": "处理中",
    "shipped": "已发货",
    "delivered": "已送达",
    "completed": "已完成",
    "canceled": "已取消",
    "requested": "已申请",
    "returning": "退货中",
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "healthy": "正常",
    "stockout": "已经缺货",
    "stockout_risk": "临近缺货",
    "replenishment_due": "需要补货",
    "slow_moving": "销售偏慢",
    "draft": "草稿",
    "awaiting_review": "待审核",
    "approved": "已批准",
    "observed": "观察中",
    "closed": "已关闭",
    "rejected": "已驳回",
    "unknown": "未知",
    "degraded": "已降级",
    "fresh": "新鲜",
    "current": "当前有效",
    "stale": "已过期",
    "superseded": "已被新证据替代",
    "future": "未来数据",
    "last_value": "最近值模型",
    "seasonal_naive_7": "七日季节性模型",
    "rolling_mean": "滚动均值模型",
    "weighted_moving_average": "加权移动平均模型",
    "ewma": "指数加权模型",
    "croston": "Croston 间歇需求模型",
    "tsb": "TSB 间歇需求模型",
    "尚未选定": "尚未选定",
    "standard": "标准",
    "withheld": "暂不提供",
    "failed": "失败",
    "partial": "部分完成",
    "passed": "通过",
    "blocked": "被阻断",
    "missing": "缺失",
    "not_required": "无需运行",
    "not_generated": "尚未生成",
    "unavailable": "不可用",
    "generated": "已生成",
    "not_applicable": "不适用",
    "inconclusive": "暂不能下结论",
    "supported": "支持",
    "ready": "就绪",
    "running": "运行中",
    "paused": "已暂停",
    "invalid": "无效",
    "limited_passed": "有限通过",
    "needs_review": "待复核",
    "collected": "已揽收",
    "in_transit": "运输中",
    "exception": "物流异常",
    "open": "待处理",
    "reviewing": "复核中",
    "ignored": "已忽略",
    "ended": "已结束",
    "true": "是",
    "false": "否",
    "advisory": "仅建议",
    "advisory_only": "仅建议",
    "valid": "有效",
    "positive_effect": "正向变化",
    "negative_effect": "负向变化",
    "no_detectable_effect": "未检测到明确差异",
}


MODEL_LABELS = frozenset(
    {
        "最近值模型",
        "七日季节性模型",
        "滚动均值模型",
        "加权移动平均模型",
        "指数加权模型",
        "Croston 间歇需求模型",
        "TSB 间歇需求模型",
    }
)


_WORKSPACE_OVERVIEW_COUNT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("pending_learning", "知识学习候选数", "知识学习候选"),
    ("pending_qa_reviews", "质检待复核数", "质检待复核"),
)


_STATUS_LABELS_BY_FIELD: dict[str, frozenset[str]] = {
    "核实结果": frozenset({"已核实", "暂无数据", "失败"}),
    "整机状态": frozenset({"正常", "不可用", "就绪"}),
    "商品状态": frozenset({"草稿", "正常", "停用", "已删除"}),
    "订单状态": frozenset(
        {"已创建", "已支付", "履约中", "已发货", "已送达", "已关闭", "已取消"}
    ),
    "支付状态": frozenset({"未支付", "已支付", "部分退款", "已退款", "已关闭"}),
    "物流状态": frozenset({"待处理", "已揽收", "运输中", "已送达", "物流异常"}),
    "预测状态": frozenset(
        {"正常", "已完成", "已降级", "失败", "部分完成", "运行中", "待处理"}
    ),
    "预测模型": MODEL_LABELS | frozenset({"尚未选定"}),
    "证据新鲜度": frozenset({"新鲜", "当前有效", "已过期", "已被新证据替代"}),
    "风险等级": frozenset({"低", "中", "高", "严重"}),
    "库存风险": frozenset({"正常", "已经缺货", "临近缺货", "需要补货", "销售偏慢"}),
    "计划质量": frozenset(
        {"标准", "已降级", "暂不提供", "有效", "无效", "通过", "被阻断", "缺失", "待复核"}
    ),
    "建议模式": frozenset({"仅建议", "暂不提供", "尚未生成"}),
    "指标质量": frozenset({"可用", "暂无数据", "缺失", "已降级", "有效", "无效"}),
    "分析状态": frozenset({"通过", "被阻断", "暂不能下结论", "支持", "失败", "已完成"}),
    "质量门禁": frozenset({"通过", "被阻断", "缺失", "无需运行"}),
    "实验状态": frozenset({"就绪", "运行中", "已完成", "已暂停", "无效"}),
    "统计结论": frozenset(
        {"正向变化", "负向变化", "未检测到明确差异", "暂不能下结论", "被阻断"}
    ),
    "建议状态": frozenset(
        {"草稿", "待审核", "已批准", "已驳回", "观察中", "已过期", "已关闭"}
    ),
    "证据状态": frozenset({"已核实", "已降级"}),
}


_STATUS_ASSERTION_ALIASES: dict[str, tuple[str, ...]] = {
    "正常": ("正常", "健康", "无异常", "没有异常", "没有需要立即处理的异常"),
    "已核实": ("已核实", "已经核实", "已确认"),
    "失败": ("失败", "出错", "未完成"),
    "不可用": ("不可用", "无法使用"),
    "缺失": ("缺失", "不完整"),
    "被阻断": ("被阻断", "阻断"),
    "暂无数据": ("暂无数据", "没有数据", "无数据", "未查到", "没有对应记录"),
    "已降级": ("已降级", "降级"),
    "通过": ("通过", "已通过"),
    "已完成": ("已完成", "完成"),
    "履约中": ("履约中", "正在履约"),
    "已支付": ("已支付", "已经支付", "支付完成"),
    "已取消": ("已取消", "已经取消", "已作废", "作废"),
    "已关闭": ("已关闭", "已经关闭"),
    "运输中": ("运输中", "正在运输", "物流运输中"),
    "新鲜": ("新鲜",),
    "当前有效": ("当前有效", "有效"),
    "已过期": ("已过期", "过期"),
    "已被新证据替代": ("已被新证据替代", "被替代"),
}


def tool_label(tool_name: str | None) -> str:
    return TOOL_LABELS.get(tool_name or "", "业务信息")


def present_observation(tool_name: str | None, observation: dict[str, Any]) -> dict[str, Any]:
    """Translate verified internal results into business language for the answer model.

    The raw observation remains available to execution/audit code, but must not be
    passed to the customer-facing answer model. Each presenter emits complete
    Chinese sentences so field names and status codes cannot leak into the reply.
    """

    handlers = {
        "get_workspace_overview": _workspace_facts,
        "get_customer_service_status": _customer_service_facts,
        "get_governance_status": _governance_facts,
        "get_channel_status": _channel_facts,
        "get_module_registry": _module_facts,
        "get_catalog_status": _product_facts,
        "get_order_management_status": _order_facts,
        "get_operations_assistant_report": _operations_facts,
        "generate_marketing_copy_draft": _copy_facts,
        "get_product_facts": _product_facts,
        "search_products": _product_search_facts,
        "get_order_facts": _order_facts,
        "get_inventory_risk": _inventory_facts,
        "get_business_metric": _metric_facts,
        "get_competitor_price_analysis": _competitive_facts,
        "get_competitive_intelligence": _competitive_facts,
        "get_marketing_diagnosis": _marketing_facts,
        "get_profit_reconciliation": _finance_facts,
        "get_listing_traffic_insights": _traffic_insight_facts,
        "get_demand_forecast": _forecast_facts,
        "get_inventory_plan": _inventory_plan_facts,
        "list_recommendations": _recommendation_facts,
        "get_recommendation_audit_trail": _recommendation_audit_facts,
    }
    data_status = observation_data_status(tool_name or "", observation)
    facts = (
        ["当前查询范围内暂无数据。"]
        if data_status == "no_data"
        else handlers.get(tool_name or "", _fallback_facts)(observation)
    )
    return {
        "查询内容": tool_label(tool_name),
        "已核实信息": facts or ["目前没有查到对应记录。"],
        "已核实状态": _status_facts(tool_name or "", observation),
        "已核实字段": critical_fact_claims(tool_name or "", observation),
    }


def observation_data_status(
    tool_name: str, observation: dict[str, Any]
) -> str:
    if tool_name == "get_business_metric":
        quality = str(observation.get("quality") or "")
        evidence_count = _number(observation.get("evidence_count"))
        if (
            quality in {"no_data", "missing"}
            or evidence_count == 0
            or observation.get("value") is None
        ):
            return "no_data"
    if tool_name == "get_inventory_risk" and not _list(observation.get("risks")):
        return "no_data"
    if tool_name == "search_products" and str(
        observation.get("resolution") or "no_match"
    ) != "resolved":
        return "no_data"
    if tool_name == "get_demand_forecast" and not _dict(observation.get("forecast")):
        return "no_data"
    if tool_name == "get_inventory_plan" and not _dict(observation.get("inventory_plan")):
        return "no_data"
    if tool_name == "get_listing_traffic_insights" and not _list(observation.get("insights")):
        return "no_data"
    if tool_name == "get_operations_assistant_report":
        record_count = _dict(observation.get("data_quality")).get("record_count")
        if record_count is not None and _number(record_count) == 0:
            return "no_data"
    return "success"


def _lower_priced_competitor_count(observation: dict[str, Any]) -> Any:
    return _dict(observation.get("summary")).get("our_price_higher")


def _inventory_entity(value: dict[str, Any]) -> tuple[str, str]:
    sku_id = str(value.get("sku_id") or "未提供编号")
    warehouse_id = str(value.get("warehouse_id") or "").strip()
    if warehouse_id:
        return f"商品 {sku_id}（仓库 {warehouse_id}）", f"{sku_id}@{warehouse_id}"
    return f"商品 {sku_id}", sku_id


def critical_fact_claims(
    tool_name: str, observation: dict[str, Any]
) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []

    def add(
        entity: str,
        entity_id: Any,
        field: str,
        value: Any,
        *,
        field_terms: str = "",
    ) -> None:
        if value in (None, ""):
            return
        claims.append(
            {
                "实体": entity,
                "实体标识": str(entity_id or ""),
                "字段": field,
                "字段语义": field_terms or field,
                "数值": str(value),
            }
        )

    if tool_name in {"get_workspace_overview", "get_customer_service_status"}:
        team = _dict(observation.get("customer_team"))
        for field, key in (
            ("客服总数", "total"),
            ("在线客服数", "online"),
            ("工作中客服数", "working"),
            ("可继续接待客服数", "available"),
        ):
            if key in team:
                add("客服团队", "customer_team", field, team.get(key))
        handoffs = _dict(observation.get("handoffs"))
        for field, key in (
            ("待处理任务数", "open"),
            ("未分配任务数", "unassigned"),
            ("即将超时任务数", "due_soon"),
            ("已超时任务数", "breached"),
        ):
            if key in handoffs:
                add("人工接待任务", "handoffs", field, handoffs.get(key))
    if tool_name == "get_workspace_overview":
        counts = _dict(_dict(observation.get("overview")).get("counts"))
        for key, field, display in _WORKSPACE_OVERVIEW_COUNT_FIELDS:
            if key in counts:
                add(
                    "工作区待办",
                    "workspace_overview",
                    field,
                    counts.get(key),
                    field_terms=f"{field} {display}",
                )
    if tool_name == "get_governance_status":
        knowledge = _dict(observation.get("knowledge"))
        add("知识库", "knowledge", "生效知识数", knowledge.get("active_count"))
        add("知识库", "knowledge", "候选知识数", knowledge.get("candidate_count"))
        add("标准处理流程", "sops", "流程数量", len(_list(observation.get("sops"))))
        add(
            "自进化候选",
            "evolution_candidates",
            "候选数量",
            len(_list(observation.get("evolution_candidates"))),
        )
    if tool_name == "get_channel_status":
        adapters = _list(observation.get("adapters"))
        add("渠道连接", "channels", "连接数量", len(adapters))
        add(
            "渠道连接",
            "channels",
            "可用连接数量",
            sum(
                str(_dict(item).get("status")) in {"active", "available", "ready"}
                for item in adapters
            ),
        )
    if tool_name == "get_module_registry":
        modules = _list(observation.get("modules"))
        add(
            "业务能力",
            "modules",
            "登记数量",
            len(modules),
            field_terms="登记数量 共登记 业务能力总数",
        )
        add(
            "业务能力",
            "modules",
            "可用数量",
            sum(str(_dict(item).get("status")) == "available" for item in modules),
            field_terms="可用数量 当前可用 可用能力",
        )
    if tool_name in {"get_catalog_status", "get_product_facts", "search_products"}:
        items = _list(observation.get("items"))
        add("商品目录", "catalog", "商品数量", len(items))
        for item in items[:8]:
            value = _dict(item)
            sku_id = value.get("sku_id")
            add(
                f"商品 {sku_id or '未提供编号'}",
                sku_id,
                "售价",
                value.get("sale_price"),
                field_terms="售价 价格 金额",
            )
    if tool_name in {"get_order_management_status", "get_order_facts"}:
        orders = _list(observation.get("orders"))
        add("订单汇总", "orders", "订单数量", len(orders))
        for item in orders[:8]:
            value = _dict(item)
            order_id = value.get("order_id")
            entity = f"订单 {order_id or '未提供编号'}"
            add(
                entity,
                order_id,
                "订单金额",
                value.get("total_amount"),
                field_terms="订单金额 金额 销售额",
            )
            add(entity, order_id, "商品件数", len(_list(value.get("lines"))))
    if tool_name == "get_inventory_risk":
        risks = _list(observation.get("risks"))
        add(
            "库存风险",
            "inventory",
            "库存记录数",
            len(risks),
            field_terms="库存记录数 共检查 库存记录",
        )
        add(
            "库存风险",
            "inventory",
            "优先关注数",
            sum(
                str(_dict(item).get("risk_level")) in {"high", "critical"}
                for item in risks
            ),
            field_terms="优先关注数 需要优先关注 优先关注",
        )
        for item in risks[:8]:
            value = _dict(item)
            entity, entity_id = _inventory_entity(value)
            add(entity, entity_id, "可用库存", value.get("available"))
            add(entity, entity_id, "预计可售天数", value.get("coverage_days"))
            add(
                entity,
                entity_id,
                "建议补货数量",
                value.get("recommended_replenishment"),
            )
    if tool_name == "get_business_metric":
        entity = str(observation.get("display_name") or "经营指标")
        add(
            entity,
            str(observation.get("metric") or "metric"),
            "指标值",
            observation.get("value"),
            field_terms=f"{entity} 指标值 金额 收入 比例 数量",
        )
        add(entity, "metric", "业务记录数", observation.get("evidence_count"))
    if tool_name in {"get_competitor_price_analysis", "get_competitive_intelligence"}:
        add(
            "竞品分析",
            "competitive",
            "可比较竞品数",
            _dict(observation.get("quality_gate")).get("eligible_competitors"),
        )
        add("竞品分析", "competitive", "价格提醒数", len(_list(observation.get("alerts"))))
        add(
            "竞品分析",
            "competitive",
            "低价竞品数",
            _lower_priced_competitor_count(observation),
        )
    if tool_name == "get_marketing_diagnosis":
        totals = _dict(observation.get("totals"))
        add("营销投放", "marketing", "投放花费", totals.get("spend"))
        add("营销投放", "marketing", "归因订单数", totals.get("attributed_orders"))
        add("营销投放", "marketing", "归因收入", totals.get("attributed_revenue"))
        add("营销投放", "marketing", "投放回报", totals.get("roas"))
        add("营销投放", "marketing", "点击率", totals.get("ctr"))
    if tool_name == "get_profit_reconciliation":
        profit = _dict(observation.get("profit"))
        add("利润核对", "profit", "销售额", profit.get("gross_sales"))
        add("利润核对", "profit", "退款金额", profit.get("approved_refunds"))
        add("利润核对", "profit", "费用", profit.get("expense_total"))
        add("利润核对", "profit", "预计利润", profit.get("management_profit"))
        add(
            "利润核对",
            "profit",
            "结算核对任务数",
            len(_list(observation.get("reconciliation_tasks"))),
        )
    if tool_name == "get_listing_traffic_insights":
        insights = _list(observation.get("insights"))
        add("流量实验洞察", observation.get("sku_id"), "分析数量", len(insights))
        for item in insights[:4]:
            insight = _dict(item)
            experiment = _dict(insight.get("experiment"))
            experiment_id = experiment.get("experiment_id")
            evidence = _dict(_dict(insight.get("analysis")).get("evidence"))
            effect = _dict(evidence.get("effect"))
            interval = _dict(evidence.get("confidence_interval"))
            entity = f"实验 {experiment_id or '未提供编号'}"
            add(entity, experiment_id, "变化值", effect.get("absolute"))
            add(entity, experiment_id, "可信区间下限", interval.get("low"))
            add(entity, experiment_id, "可信区间上限", interval.get("high"))
    if tool_name == "get_demand_forecast":
        forecast = _dict(observation.get("forecast"))
        sku_id = forecast.get("sku_id")
        entity = f"需求预测 {sku_id or ''}".strip()
        points = _list(forecast.get("points"))
        add(entity, sku_id, "预测日数量", len(points))
        if points:
            first = _dict(points[0])
            add(entity, sku_id, "首日 P50", first.get("p50"))
            add(entity, sku_id, "首日 P80", first.get("p80"))
            add(entity, sku_id, "首日 P95", first.get("p95"))
    if tool_name == "get_inventory_plan":
        plan = _dict(observation.get("inventory_plan"))
        sku_id = plan.get("sku_id")
        entity = f"库存计划 {sku_id or ''}".strip()
        add(
            entity,
            sku_id,
            "建议数量",
            plan.get("recommended_order_qty")
            if plan.get("recommended_order_qty") is not None
            else plan.get("recommended_qty"),
        )
    if tool_name == "list_recommendations":
        add(
            "商品经营建议",
            "recommendations",
            "建议数量",
            len(_list(observation.get("items"))),
        )
    if tool_name == "get_recommendation_audit_trail":
        add(
            "建议审计记录",
            observation.get("recommendation_id") or "recommendation_audit",
            "审计记录数",
            len(_list(observation.get("items"))),
        )
    if tool_name == "generate_marketing_copy_draft":
        add(
            "营销文案草稿",
            "copy_draft",
            "候选文案数",
            len(_list(observation.get("variants"))),
        )
    return claims


def critical_fact_values(product_view: dict[str, Any]) -> list[str]:
    values: list[str] = []
    identifier_pattern = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z][A-Za-z0-9-]*\b")
    number_pattern = re.compile(
        r"(?<![A-Za-z0-9-])[+-]?\d+(?:\.\d+)?(?![A-Za-z0-9-])"
    )
    for fact in product_view.get("已核实信息") or []:
        text = str(fact)
        for match in [*identifier_pattern.findall(text), *number_pattern.findall(text)]:
            if match not in values:
                values.append(match)
    return values


def _numeric_key(value: str) -> Decimal | None:
    return Decimal(value.replace(",", ""))


def _critical_tokens(text: str) -> tuple[set[str], set[Decimal]]:
    values = {str(value) for value in critical_fact_values({"已核实信息": [text]})}
    identifiers = {
        value
        for value in values
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", value)
    }
    numbers = {
        number
        for value in values - identifiers
        if (number := _numeric_key(value)) is not None
    }
    return identifiers, numbers


def _status_facts(tool_name: str, observation: dict[str, Any]) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []

    def add(entity: str, field: str, value: Any) -> None:
        if value in (None, ""):
            return
        facts.append(
            {
                "主题": f"{entity}的{field}",
                "实体": entity,
                "字段": field,
                "状态": _status(value),
            }
        )

    add(tool_label(tool_name), "核实结果", observation_data_status(tool_name, observation))

    if tool_name == "get_workspace_overview":
        add("整机", "整机状态", "active" if observation.get("ready") else "unavailable")
    elif tool_name in {"get_catalog_status", "get_product_facts", "search_products"}:
        for item in _list(observation.get("items"))[:8]:
            value = _dict(item)
            entity = f"商品 {value.get('sku_id') or '未提供编号'}"
            add(entity, "商品状态", value.get("status"))
    elif tool_name in {"get_order_management_status", "get_order_facts"}:
        for item in _list(observation.get("orders"))[:8]:
            value = _dict(item)
            entity = f"订单 {value.get('order_id') or '未提供编号'}"
            add(entity, "订单状态", value.get("order_status"))
            add(entity, "支付状态", value.get("payment_status"))
            add(entity, "物流状态", _dict(value.get("logistics")).get("status"))
    elif tool_name == "get_inventory_risk":
        risks = [_dict(item) for item in _list(observation.get("risks"))[:8]]
        if risks:
            severity = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            highest = max(
                risks,
                key=lambda item: severity.get(str(item.get("risk_level")), -1),
            )
            add("库存风险", "风险等级", highest.get("risk_level"))
        for value in risks:
            entity, _ = _inventory_entity(value)
            add(entity, "库存风险", value.get("risk_code"))
            add(entity, "风险等级", value.get("risk_level"))
    elif tool_name == "get_demand_forecast":
        forecast = _dict(observation.get("forecast"))
        entity = f"需求预测 {forecast.get('sku_id') or ''}".strip()
        add(entity, "预测状态", forecast.get("status"))
        add(entity, "预测模型", forecast.get("champion_model"))
        freshness = _dict(observation.get("freshness"))
        add(entity, "证据新鲜度", freshness.get("status"))
    elif tool_name == "get_inventory_plan":
        plan = _dict(observation.get("inventory_plan"))
        entity = f"库存计划 {plan.get('sku_id') or ''}".strip()
        add(entity, "风险等级", plan.get("risk_level"))
        add(
            entity,
            "计划质量",
            plan.get("effective_plan_quality") or plan.get("plan_quality"),
        )
        add(
            entity,
            "建议模式",
            plan.get("quantity_status") or plan.get("action_mode"),
        )
        freshness = _dict(plan.get("freshness") or observation.get("freshness"))
        add(entity, "证据新鲜度", freshness.get("status"))
    elif tool_name == "get_business_metric":
        entity = str(observation.get("display_name") or "经营指标")
        add(entity, "指标质量", observation.get("quality"))
    elif tool_name in {
        "get_listing_traffic_insights",
        "get_competitive_intelligence",
        "get_competitor_price_analysis",
    }:
        add(tool_label(tool_name), "分析状态", observation.get("status"))
        add(
            tool_label(tool_name),
            "证据新鲜度",
            _dict(observation.get("freshness")).get("status"),
        )
        add(
            tool_label(tool_name),
            "质量门禁",
            _dict(observation.get("quality_gate")).get("status"),
        )
        for index, item in enumerate(_list(observation.get("insights"))[:6], start=1):
            insight = _dict(item)
            experiment = _dict(insight.get("experiment"))
            experiment_id = str(experiment.get("experiment_id") or index)
            entity = f"实验 {experiment_id}"
            add(entity, "实验状态", experiment.get("status"))
            analysis = _dict(insight.get("analysis"))
            evidence = _dict(analysis.get("evidence"))
            add(
                entity,
                "质量门禁",
                _dict(evidence.get("quality_gate")).get("status"),
            )
            add(
                entity,
                "统计结论",
                evidence.get("statistical_conclusion"),
            )
            add(
                entity,
                "证据新鲜度",
                _dict(insight.get("freshness") or analysis.get("freshness")).get("status"),
            )
    elif tool_name in {"list_recommendations", "get_recommendation_audit_trail"}:
        for item in _list(observation.get("items"))[:6]:
            value = _dict(item)
            identifier = str(
                value.get("recommendation_id") or value.get("id") or "建议"
            )
            entity = f"商品经营建议 {identifier}"
            add(entity, "建议状态", value.get("state"))
            if value.get("degraded"):
                add(entity, "证据状态", "degraded")
    return facts


def _verified_source(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        product_view = result.get("result")
        if not isinstance(product_view, dict):
            product_view = {}
        status = str(result.get("status") or "success")
        status_facts = list(
            product_view.get("已核实状态")
            or result.get("status_facts")
            or []
        )
        if not status_facts:
            entity = str(result.get("tool_label") or "业务信息")
            status_facts = [
                {
                    "主题": f"{entity}的核实结果",
                    "实体": entity,
                    "字段": "核实结果",
                    "状态": _status(
                        "no_data"
                        if status == "no_data"
                        else "failed"
                        if status != "success"
                        else "success"
                    ),
                }
            ]
        return {
            "status": status,
            "objective": str(result.get("objective") or ""),
            "tool_label": str(result.get("tool_label") or ""),
            "facts": [str(fact) for fact in product_view.get("已核实信息") or []],
            "status_facts": status_facts,
            "field_claims": list(
                product_view.get("已核实字段")
                or result.get("field_claims")
                or []
            ),
        }
    status = str(getattr(result, "status", "success"))
    status_facts = list(getattr(result, "status_facts", []) or [])
    if not status_facts:
        entity = str(getattr(result, "tool_label", "业务信息"))
        status_facts = [
            {
                "主题": f"{entity}的核实结果",
                "实体": entity,
                "字段": "核实结果",
                "状态": _status(
                    "no_data"
                    if status == "no_data"
                    else "failed"
                    if status != "success"
                    else "success"
                ),
            }
        ]
    return {
        "status": status,
        "objective": str(getattr(result, "objective", "")),
        "tool_label": str(getattr(result, "tool_label", "")),
        "facts": [
            str(fact)
            for fact in getattr(result, "verified_facts", [])
            if str(fact)
        ],
        "status_facts": status_facts,
        "field_claims": list(getattr(result, "field_claims", []) or []),
    }


def _semantic_words(value: str) -> set[str]:
    words: set[str] = set()
    stop_words = {"目前", "当前", "已经", "可以", "相关", "其中"}
    for word in re.findall(r"[A-Za-z0-9_-]+|[\u4e00-\u9fff]{2,}", value):
        if word in stop_words or word.isdigit():
            continue
        words.add(word)
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            words.update(word[index : index + 2] for index in range(len(word) - 1))
    return words


def _answer_sentences(answer: str) -> list[str]:
    sentences: list[str] = []
    for raw in re.split(r"[。！？!?；;\n]+", answer):
        sentence = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", raw).strip()
        if sentence:
            sentences.append(sentence)
    return sentences


def _answer_clauses(sentence: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"[,，:：]|(?:并且|同时|以及|而是|但是|但)", sentence)
        if clause.strip()
    ]


def _scope_match_score(text: str, scope: str) -> int:
    normalized_text = re.sub(r"\s+", "", text).lower()
    phrases = [item for item in re.split(r"\s+", scope) if item]
    score = 0
    for phrase in phrases:
        normalized_phrase = re.sub(r"\s+", "", phrase).lower()
        if normalized_phrase and normalized_phrase in normalized_text:
            score = max(score, 100 + len(normalized_phrase))

    ignored = {"状态", "数量", "结果", "信息", "当前", "业务"}
    overlap = (
        _semantic_words(text)
        & _semantic_words(scope)
        - ignored
    )
    if overlap:
        score = max(score, max(len(word) for word in overlap))
    return score


def _status_label_mentions(
    text: str, domain: frozenset[str]
) -> tuple[set[str], set[str]]:
    matches: list[tuple[int, int, str, bool]] = []
    for label in domain:
        for alias in _STATUS_ASSERTION_ALIASES.get(label, (label,)):
            start = 0
            while True:
                position = text.find(alias, start)
                if position < 0:
                    break
                if len(alias) == 1 and not re.search(
                    r"(?:为|是|等级|风险|[:：])\s*$", text[:position]
                ):
                    start = position + len(alias)
                    continue
                following = text[position + len(alias) : position + len(alias) + 1]
                if following in {"度", "性"}:
                    start = position + len(alias)
                    continue
                prefix = text[:position]
                negated = bool(
                    re.search(
                        r"(?:不|未|没有|暂无|无法|不能|不可|不应|并非|不是|不算)"
                        r"[^,，。！？!?；;]*$",
                        prefix,
                    )
                )
                matches.append((position, position + len(alias), label, negated))
                start = position + len(alias)

    selected: list[tuple[int, int, str, bool]] = []
    for match in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
        if any(match[0] < item[1] and item[0] < match[1] for item in selected):
            continue
        selected.append(match)
    return (
        {item[2] for item in selected if not item[3]},
        {item[2] for item in selected if item[3]},
    )


def _asserted_status_labels(text: str, domain: frozenset[str]) -> set[str]:
    return _status_label_mentions(text, domain)[0]


def _looks_like_status_claim(text: str) -> bool:
    return bool(
        re.search(
            r"状态|作废|取消|关闭|暂停|冻结|失效|有效|过期|履约|支付|退款|"
            r"发货|送达|运输|异常|正常|健康|可用|不可用|就绪|缺货|补货|"
            r"风险|降级|阻断|完成|运行中|待处理|草稿|审核|批准|驳回",
            text,
        )
    )


def _unique_entity_context(
    identifiers: set[str], entity_identifier_sets: list[set[str]]
) -> set[str]:
    if not identifiers:
        return set()
    matches = {
        frozenset(entity_identifiers)
        for entity_identifiers in entity_identifier_sets
        if identifiers.issubset(entity_identifiers)
    }
    return identifiers if len(matches) == 1 else set()


def answer_preserves_critical_values(
    answer: str, results: list[Any], *, require_all: bool = True
) -> bool:
    sources = [_verified_source(result) for result in results]
    sentences = _answer_sentences(answer)
    source_words = [
        _semantic_words(
            " ".join(
                [
                    source["objective"],
                    source["tool_label"],
                    *source["facts"],
                    *[
                        str(item.get("主题") or item.get("subject") or "")
                        for item in source["status_facts"]
                        if isinstance(item, dict)
                    ],
                ]
            )
        )
        for source in sources
    ]
    source_scopes = [
        f"{source['objective']} {source['tool_label']}" for source in sources
    ]
    source_tokens: list[tuple[set[str], set[Decimal]]] = []
    required_source_tokens: list[tuple[set[str], set[Decimal]]] = []
    for source in sources:
        identifiers, _ = _critical_tokens(
            " ".join(
                [
                    source["objective"],
                    source["tool_label"],
                    *source["facts"],
                    *[
                        " ".join(
                            [
                                str(item.get("主题") or item.get("subject") or ""),
                                str(item.get("实体") or item.get("entity") or ""),
                            ]
                        )
                        for item in source["status_facts"]
                        if isinstance(item, dict)
                    ],
                ]
            )
        )
        numbers: set[Decimal] = set()
        required_identifiers: set[str] = set()
        required_numbers: set[Decimal] = set()
        if source["status"] == "success":
            for fact in source["facts"]:
                fact_identifiers, fact_numbers = _critical_tokens(fact)
                required_identifiers.update(fact_identifiers)
                required_numbers.update(fact_numbers)
                numbers.update(fact_numbers)
        for item in source["field_claims"]:
            if not isinstance(item, dict):
                continue
            claim_identifiers, _ = _critical_tokens(
                " ".join(
                    [
                        str(item.get("实体") or item.get("entity") or ""),
                        str(
                            item.get("实体标识")
                            or item.get("entity_id")
                            or ""
                        ),
                    ]
                )
            )
            identifiers.update(claim_identifiers)
        source_tokens.append((identifiers, numbers))
        required_source_tokens.append((required_identifiers, required_numbers))

    seen_tokens: list[tuple[set[str], set[Decimal]]] = [(set(), set()) for _ in sources]
    seen_no_data_sources: set[int] = set()
    sentence_sources: list[set[int]] = []
    for sentence in sentences:
        identifiers, numbers = _critical_tokens(sentence)
        sentence_words = _semantic_words(sentence)
        source_scores = [
            _scope_match_score(sentence, scope) for scope in source_scopes
        ]
        best_source_score = max(source_scores, default=0)
        word_matched = (
            [
                index
                for index, score in enumerate(source_scores)
                if score == best_source_score
            ]
            if best_source_score > 0
            else [
                index
                for index, words in enumerate(source_words)
                if sentence_words & words
            ]
        )
        compatible: set[int] = set()
        candidate_indexes = (
            word_matched
            if word_matched
            else range(len(sources))
            if identifiers
            else ()
        )
        for index in candidate_indexes:
            source = sources[index]
            allowed_identifiers, allowed_numbers = source_tokens[index]
            if source["status"] != "success":
                if not numbers and identifiers.issubset(allowed_identifiers):
                    compatible.add(index)
                continue
            if identifiers.issubset(allowed_identifiers) and numbers.issubset(
                allowed_numbers
            ):
                compatible.add(index)
        if identifiers or numbers:
            if not compatible:
                return False
        else:
            compatible.update(word_matched)
        sentence_sources.append(compatible)
        for index in compatible:
            seen_tokens[index][0].update(identifiers)
            seen_tokens[index][1].update(numbers)
        if _asserted_status_labels(sentence, frozenset({"暂无数据"})):
            seen_no_data_sources.update(
                index
                for index in compatible
                if sources[index]["status"] == "no_data"
            )

        matched_no_data = any(
            sources[index]["status"] != "success" for index in word_matched
        )
        matched_success = any(
            sources[index]["status"] == "success" for index in compatible
        )
        if matched_no_data and not matched_success:
            if (
                numbers
                or re.search(r"(?:为|是|有|共|合计)\s*(?:0|零)", sentence)
            ):
                return False

    field_claims: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources):
        if source["status"] != "success":
            continue
        for item in source["field_claims"]:
            if not isinstance(item, dict):
                continue
            value_numbers = _critical_tokens(
                str(item.get("数值") or item.get("value") or "")
            )[1]
            if len(value_numbers) != 1:
                continue
            entity = str(item.get("实体") or item.get("entity") or "")
            entity_id = str(
                item.get("实体标识") or item.get("entity_id") or ""
            )
            field = str(item.get("字段") or item.get("field") or "")
            field_scope = str(
                item.get("字段语义") or item.get("field_terms") or field
            )
            if entity and field:
                field_claims.append(
                    {
                        "source_index": source_index,
                        "entity": entity,
                        "entity_id": entity_id,
                        "entity_identifiers": _critical_tokens(
                            f"{entity_id} {entity}"
                        )[0],
                        "field": field,
                        "field_scope": f"{field} {field_scope}",
                        "value": next(iter(value_numbers)),
                    }
                )

    known_entity_identifiers = {
        identifier
        for claim in field_claims
        for identifier in claim["entity_identifiers"]
    }
    for sentence_index, sentence in enumerate(sentences):
        sentence_identifiers = _critical_tokens(sentence)[0] & known_entity_identifiers
        inherited_identifiers = _unique_entity_context(
            sentence_identifiers,
            [claim["entity_identifiers"] for claim in field_claims],
        )
        for clause in _answer_clauses(sentence):
            _, numbers = _critical_tokens(clause)
            if not numbers:
                continue
            explicit_identifiers = (
                _critical_tokens(clause)[0] & known_entity_identifiers
            )
            entity_identifiers = explicit_identifiers or inherited_identifiers
            source_claims = [
                claim
                for claim in field_claims
                if (
                    not sentence_sources[sentence_index]
                    or claim["source_index"] in sentence_sources[sentence_index]
                )
            ]
            relevant_claims = [
                claim
                for claim in source_claims
                if (
                    not entity_identifiers
                    or entity_identifiers.issubset(claim["entity_identifiers"])
                )
            ]
            represented_numbers = {claim["value"] for claim in source_claims}
            for number in numbers & represented_numbers:
                scored = [
                    (claim, _scope_match_score(clause, claim["field_scope"]))
                    for claim in relevant_claims
                ]
                best_score = max((score for _, score in scored), default=0)
                selected = [
                    claim
                    for claim, score in scored
                    if score == best_score and (best_score > 0 or len(scored) == 1)
                ]
                slots = {
                    (claim["source_index"], claim["entity_id"] or claim["entity"], claim["field"])
                    for claim in selected
                }
                if len(slots) != 1 or not any(
                    claim["value"] == number for claim in selected
                ):
                    return False

    status_facts: list[dict[str, str]] = []
    for source_index, source in enumerate(sources):
        for item in source["status_facts"]:
            if isinstance(item, dict):
                subject = str(item.get("主题") or item.get("subject") or "")
                entity = str(item.get("实体") or item.get("entity") or subject)
                field = str(item.get("字段") or item.get("field") or "")
                label = str(item.get("状态") or item.get("label") or "")
                domain = _STATUS_LABELS_BY_FIELD.get(field)
                if subject and entity and field and label and domain and label in domain:
                    status_facts.append(
                        {
                            "subject": subject,
                            "entity": entity,
                            "field": field,
                            "label": label,
                            "source_index": str(source_index),
                            "entity_identifiers": " ".join(
                                sorted(_critical_tokens(entity)[0])
                            ),
                            "scope": " ".join(
                                [
                                    entity,
                                    subject,
                                    source["objective"] if field == "核实结果" else "",
                                    source["tool_label"] if field == "核实结果" else "",
                                ]
                            ),
                        }
                    )
                elif label:
                    return False
    known_status_identifiers = {
        identifier
        for item in status_facts
        for identifier in item["entity_identifiers"].split()
        if identifier
    }
    for sentence_index, sentence in enumerate(sentences):
        sentence_identifiers = _critical_tokens(sentence)[0] & known_status_identifiers
        inherited_identifiers = _unique_entity_context(
            sentence_identifiers,
            [
                set(item["entity_identifiers"].split())
                for item in status_facts
                if item["entity_identifiers"]
            ],
        )
        for clause in _answer_clauses(sentence):
            explicit_identifiers = (
                _critical_tokens(clause)[0] & known_status_identifiers
            )
            entity_identifiers = explicit_identifiers or inherited_identifiers
            matching = [
                item
                for item in status_facts
                if (
                    not sentence_sources[sentence_index]
                    or int(item["source_index"]) in sentence_sources[sentence_index]
                )
                and (
                    not entity_identifiers
                    or entity_identifiers.issubset(
                        set(item["entity_identifiers"].split())
                    )
                )
            ]
            candidates: list[tuple[dict[str, str], str, int]] = []
            negative_candidates: list[tuple[dict[str, str], str, int]] = []
            for item in matching:
                domain = _STATUS_LABELS_BY_FIELD[item["field"]]
                field_scope = item["field"]
                score = _scope_match_score(clause, field_scope)
                if item["field"] == "核实结果":
                    score = max(score, _scope_match_score(clause, item["scope"]))
                asserted_labels, negated_labels = _status_label_mentions(clause, domain)
                for asserted in asserted_labels:
                    candidates.append((item, asserted, score))
                for negated in negated_labels:
                    negative_candidates.append((item, negated, score))
            all_candidates = [*candidates, *negative_candidates]
            if any(
                item["field"] != "核实结果" for item, _, _ in all_candidates
            ):
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate[0]["field"] != "核实结果"
                ]
                negative_candidates = [
                    candidate
                    for candidate in negative_candidates
                    if candidate[0]["field"] != "核实结果"
                ]
            if negative_candidates:
                best_negative_score = max(
                    score for _, _, score in negative_candidates
                )
                selected_negative = [
                    (item, negated)
                    for item, negated, score in negative_candidates
                    if score == best_negative_score
                ]
                if any(
                    item["label"] == negated
                    for item, negated in selected_negative
                ):
                    return False
            if not candidates:
                if (
                    matching
                    and not _critical_tokens(clause)[1]
                    and _looks_like_status_claim(clause)
                ):
                    return False
                continue
            best_score = max(score for _, _, score in candidates)
            selected = [
                (item, asserted)
                for item, asserted, score in candidates
                if score == best_score
            ]
            if any(item["label"] != asserted for item, asserted in selected):
                return False

    required_no_data_sources = {
        index for index, source in enumerate(sources) if source["status"] == "no_data"
    }
    if not required_no_data_sources.issubset(seen_no_data_sources):
        return False

    if require_all:
        for index, (required_identifiers, required_numbers) in enumerate(
            required_source_tokens
        ):
            if sources[index]["status"] != "success":
                continue
            seen_identifiers, seen_numbers = seen_tokens[index]
            if not required_identifiers.issubset(seen_identifiers):
                return False
            if not required_numbers.issubset(seen_numbers):
                return False
    return True


def _status_assertion_present(sentence: str, label: str) -> bool:
    return label in _asserted_status_labels(sentence, frozenset({label}))


def observation_summary(product_view: dict[str, Any]) -> str:
    facts = product_view.get("已核实信息") or []
    if not facts:
        return "目前没有查到对应记录"
    return str(facts[0])


def _number(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status(value: Any) -> str:
    if value is True:
        raw = "true"
    elif value is False:
        raw = "false"
    else:
        raw = str(value or "未知").strip()
    return STATUS_LABELS.get(raw, "未知")


def _customer_team_fact(observation: dict[str, Any]) -> str | None:
    team = _dict(observation.get("customer_team"))
    if not team:
        return None
    return (
        f"总共 {_number(team.get('total'))} 位客服，在线 {_number(team.get('online'))} 位，"
        f"正在工作 {_number(team.get('working'))} 位，其中可继续接待 "
        f"{_number(team.get('available'))} 位。"
    )


def _handoff_fact(observation: dict[str, Any]) -> str | None:
    handoffs = _dict(observation.get("handoffs"))
    if not handoffs:
        return None
    return (
        f"人工接待任务目前待处理 {_number(handoffs.get('open'))} 个，其中未分配 "
        f"{_number(handoffs.get('unassigned'))} 个、即将超时 {_number(handoffs.get('due_soon'))} 个、"
        f"已经超时 {_number(handoffs.get('breached'))} 个。"
    )


def _workspace_facts(observation: dict[str, Any]) -> list[str]:
    facts = ["整机当前可以正常提供服务。" if observation.get("ready") else "整机当前存在未就绪项目，需要检查。"]
    team = _customer_team_fact(observation)
    handoff = _handoff_fact(observation)
    if team:
        facts.append(team)
    if handoff:
        facts.append(handoff)
    overview = _dict(observation.get("overview"))
    counts = _dict(overview.get("counts"))
    if counts:
        facts.append(
            "，".join(
                f"{display}有 {_number(counts.get(key))} 条"
                for key, _, display in _WORKSPACE_OVERVIEW_COUNT_FIELDS
            )
            + "。"
        )
    return facts


def _customer_service_facts(observation: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    team = _customer_team_fact(observation)
    handoff = _handoff_fact(observation)
    if team:
        facts.append(team)
    if handoff:
        facts.append(handoff)
    conversations = _list(observation.get("recent_conversations"))
    facts.append(f"本次已查看最近 {len(conversations)} 个客服会话。")
    dispatch = _dict(observation.get("dispatch"))
    alerts = _dict(dispatch.get("alerts"))
    alert_count = sum(_number(value) for value in alerts.values())
    if alert_count:
        facts.append(f"自动派单目前有 {alert_count} 个需要关注的提醒。")
    return facts


def _governance_facts(observation: dict[str, Any]) -> list[str]:
    knowledge = _dict(observation.get("knowledge"))
    sops = _list(observation.get("sops"))
    candidates = _list(observation.get("evolution_candidates"))
    return [
        f"知识库已有 {_number(knowledge.get('active_count'))} 条生效知识，另有 {_number(knowledge.get('candidate_count'))} 条候选知识等待审核。",
        f"当前维护了 {len(sops)} 套标准处理流程，有 {len(candidates)} 条自进化候选等待评估。",
    ]


def _channel_facts(observation: dict[str, Any]) -> list[str]:
    adapters = _list(observation.get("adapters"))
    available = sum(str(_dict(item).get("status")) in {"active", "available", "ready"} for item in adapters)
    outbox = _dict(observation.get("outbox"))
    waiting = sum(
        _number(outbox.get(key))
        for key in ("pending", "waiting", "retrying", "leased")
    )
    facts = [f"共接入 {len(adapters)} 个渠道连接，其中 {available} 个当前可用。"]
    facts.append(f"发送队列中有 {waiting} 条消息正在等待或重试。")
    return facts


def _module_facts(observation: dict[str, Any]) -> list[str]:
    modules = _list(observation.get("modules"))
    available = [
        str(_dict(item).get("display_name") or "未命名能力")
        for item in modules
        if str(_dict(item).get("status")) == "available"
    ]
    facts = [f"共登记 {len(modules)} 项业务能力，其中 {len(available)} 项当前可用。"]
    if available:
        facts.append("可用能力包括：" + "、".join(available[:10]) + "。")
    return facts


def _operations_facts(observation: dict[str, Any]) -> list[str]:
    summary = [str(item) for item in _list(observation.get("summary")) if str(item).strip()]
    findings = [
        item
        for item in _list(observation.get("findings"))
        if str(_dict(item).get("code") or "") != "no_data"
    ]
    facts = summary[:4]
    if findings:
        facts.append(f"分析中发现 {len(findings)} 个值得关注的经营信号。")
    return facts


def _copy_facts(observation: dict[str, Any]) -> list[str]:
    variants = _list(observation.get("variants"))
    facts = [f"已生成 {len(variants)} 条候选文案；全部需要人工复核，目前尚未发布。"]
    for index, item in enumerate(variants[:6], start=1):
        body = str(_dict(item).get("body") or "").strip()
        if body:
            facts.append(f"候选文案 {index}：{body}")
    return facts


def _product_line(item: Any) -> str:
    value = _dict(item)
    title = str(value.get("title") or "未命名商品")
    sku = str(value.get("sku_id") or "未提供")
    price = value.get("sale_price")
    currency = str(value.get("currency") or "CNY")
    price_text = f"，售价 {price} {currency}" if price not in {None, ""} else ""
    return f"{title}，商品编号 {sku}{price_text}，状态为{_status(value.get('status'))}。"


def _product_facts(observation: dict[str, Any]) -> list[str]:
    items = _list(observation.get("items"))
    return [f"共找到 {len(items)} 个商品。", *[_product_line(item) for item in items[:8]]]


def _product_search_facts(observation: dict[str, Any]) -> list[str]:
    items = _list(observation.get("items"))
    resolution = _status(observation.get("resolution"))
    return [f"商品搜索结果：{resolution}，共找到 {len(items)} 个商品。", *[_product_line(item) for item in items[:8]]]


def _order_facts(observation: dict[str, Any]) -> list[str]:
    orders = _list(observation.get("orders"))
    facts = [f"共找到 {len(orders)} 个订单。"]
    for item in orders[:6]:
        order = _dict(item)
        facts.append(
            f"订单 {order.get('order_id') or '未提供编号'}：订单状态为{_status(order.get('order_status'))}，"
            f"支付状态为{_status(order.get('payment_status'))}，金额 {order.get('total_amount') or '0'} "
            f"{order.get('currency') or 'CNY'}，包含 {len(_list(order.get('lines')))} 件商品。"
        )
        logistics = _dict(order.get("logistics"))
        if logistics:
            facts.append(
                f"该订单物流由 {logistics.get('carrier') or '承运方未记录'} 配送，当前{_status(logistics.get('status'))}；"
                f"最近进展：{logistics.get('last_event') or '暂无更新'}。"
            )
    return facts


def _inventory_facts(observation: dict[str, Any]) -> list[str]:
    risks = _list(observation.get("risks"))
    high = sum(str(_dict(item).get("risk_level")) in {"high", "critical"} for item in risks)
    facts = [f"共检查 {len(risks)} 个库存记录，其中 {high} 个需要优先关注。"]
    for item in risks[:8]:
        risk = _dict(item)
        entity, _ = _inventory_entity(risk)
        facts.append(
            f"{entity}：{_status(risk.get('risk_code'))}，"
            f"可用库存 {risk.get('available') or '0'}，预计可售 {risk.get('coverage_days') or '无法估算'} 天，"
            f"建议补货 {risk.get('recommended_replenishment') or '0'} 件。"
        )
    return facts


def _metric_facts(observation: dict[str, Any]) -> list[str]:
    name = str(observation.get("display_name") or "经营指标")
    value = observation.get("value", 0)
    unit = str(observation.get("unit") or "")
    if unit == "currency":
        shown = f"{value} 元"
    elif unit == "ratio":
        try:
            shown = f"{float(value) * 100:.2f}%"
        except (TypeError, ValueError):
            shown = str(value)
    else:
        shown = f"{value} 个" if unit == "count" else str(value)
    facts = [f"{name}为 {shown}。"]
    facts.append(f"该结果依据 {_number(observation.get('evidence_count'))} 条业务记录计算。")
    return facts


def _competitive_facts(observation: dict[str, Any]) -> list[str]:
    summary = _dict(observation.get("summary"))
    observations = _list(observation.get("observations"))
    alerts = _list(observation.get("alerts"))
    trends = _list(observation.get("trends"))
    eligible = _number(_dict(observation.get("quality_gate")).get("eligible_competitors"), len(observations))
    facts = [f"本次有 {eligible} 个已核实竞品可用于比较，另有 {len(alerts)} 个价格提醒。"]
    lower = _lower_priced_competitor_count(observation)
    if lower is not None:
        facts.append(f"其中有 {_number(lower)} 个竞品价格低于本品。")
    if trends:
        facts.append(f"已形成 {len(trends)} 组可参考的价格趋势。")
    return facts


def _marketing_facts(observation: dict[str, Any]) -> list[str]:
    totals = _dict(observation.get("totals"))
    findings = _list(observation.get("findings"))
    facts = [
        f"本期投放花费 {totals.get('spend') or '0'} 元，带来 {totals.get('attributed_orders') or 0} 个归因订单，"
        f"归因收入 {totals.get('attributed_revenue') or '0'} 元。"
    ]
    roas = totals.get("roas")
    ctr = totals.get("ctr")
    if roas is not None or ctr is not None:
        ctr_text = "暂无" if ctr is None else f"{float(ctr) * 100:.2f}%"
        facts.append(f"投放回报为 {roas or '暂无'}，点击率为 {ctr_text}。")
    recommendations = [str(_dict(item).get("recommendation") or "").strip() for item in findings]
    recommendations = [item for item in recommendations if item]
    if recommendations:
        facts.append("建议：" + "；".join(recommendations[:3]) + "。")
    return facts


def _finance_facts(observation: dict[str, Any]) -> list[str]:
    profit = _dict(observation.get("profit"))
    tasks = _list(observation.get("reconciliation_tasks"))
    currency = str(profit.get("currency") or "CNY")
    facts = [
        f"管理口径销售额 {profit.get('gross_sales') or '0'} {currency}，退款 {profit.get('approved_refunds') or '0'} {currency}，"
        f"费用 {profit.get('expense_total') or '0'} {currency}，预计利润 {profit.get('management_profit') or '0'} {currency}。",
        f"目前有 {len(tasks)} 个结算核对任务。该利润仅用于经营管理参考，不是财务报表或付款指令。",
    ]
    return facts


def _traffic_insight_facts(observation: dict[str, Any]) -> list[str]:
    sku_id = str(observation.get("sku_id") or "未提供编号")
    insights = _list(observation.get("insights"))
    facts = [f"商品 {sku_id} 已找到 {len(insights)} 份固化的流量实验分析。"]
    if not insights:
        facts.append("目前没有可供解读的实验结果；需要先在流量实验页面明确运行分析。")
        return facts
    for item in insights[:4]:
        insight = _dict(item)
        experiment = _dict(insight.get("experiment"))
        analysis = _dict(insight.get("analysis"))
        evidence = _dict(analysis.get("evidence"))
        effect = _dict(evidence.get("effect"))
        interval = _dict(evidence.get("confidence_interval"))
        effect_text = effect.get("absolute")
        direction = str(effect.get("direction") or "暂无明确方向")
        interval_text = ""
        if interval.get("low") is not None and interval.get("high") is not None:
            interval_text = f"，可信区间为 {interval.get('low')} 到 {interval.get('high')}"
        facts.append(
            f"实验 {experiment.get('experiment_id') or '未提供编号'} 关注"
            f"{experiment.get('primary_metric') or '流量指标'}，当前方向为 {direction}"
            f"，变化值为 {effect_text if effect_text is not None else '暂无'}{interval_text}。"
        )
    facts.append("以上只复述已固化的统计证据，不会重算统计，也不代表平台权重或因果机制。")
    return facts


def _forecast_facts(observation: dict[str, Any]) -> list[str]:
    forecast = _dict(observation.get("forecast"))
    if not forecast:
        return ["目前没有已固化的需求预测，不能据此判断未来销量。"]
    status = _status(forecast.get("status"))
    champion = _status(forecast.get("champion_model") or "尚未选定")
    points = _list(forecast.get("points"))
    facts = [f"最新需求预测状态为{status}，当前采用 {champion}。"]
    if points:
        first = _dict(points[0])
        facts.append(
            f"已固化 {len(points)} 个预测日，首日 P50 为 {first.get('p50', '未知')}，"
            f"P80 为 {first.get('p80', '未知')}，P95 为 {first.get('p95', '未知')}。"
        )
    freshness = _dict(observation.get("freshness"))
    if freshness:
        facts.append(f"预测证据新鲜度为{_status(freshness.get('status'))}。")
    return facts


def _inventory_plan_facts(observation: dict[str, Any]) -> list[str]:
    plan = _dict(observation.get("inventory_plan"))
    if not plan:
        return ["当前没有已固化的库存计划，不能据此生成采购或调拨动作。"]
    risk = _status(plan.get("risk_level"))
    quantity = plan.get("recommended_order_qty")
    if quantity is None:
        quantity = plan.get("recommended_qty")
    quantity_text = "暂不提供" if quantity is None else str(quantity)
    facts = [f"当前库存计划风险为{risk}，建议数量为 {quantity_text}。"]
    quality = plan.get("effective_plan_quality") or plan.get("plan_quality")
    if quality:
        facts.append(f"库存计划质量为{_status(quality)}。")
    if plan.get("expected_stockout_date"):
        facts.append(f"预计缺货日期为 {plan['expected_stockout_date']}。")
    quantity_status = plan.get("quantity_status") or plan.get("action_mode")
    if quantity_status:
        facts.append(
            f"该计划只提供{_status(quantity_status)}建议，不会直接创建采购动作。"
        )
    freshness = _dict(plan.get("freshness") or observation.get("freshness"))
    if freshness.get("status"):
        facts.append(f"库存计划证据新鲜度为{_status(freshness['status'])}。")
    return facts


def _recommendation_facts(observation: dict[str, Any]) -> list[str]:
    items = _list(observation.get("items"))
    facts = [f"当前共有 {len(items)} 条商品经营建议。"]
    for item in items[:6]:
        value = _dict(item)
        recommendation_id = value.get("recommendation_id") or value.get("id") or "未提供编号"
        recommendation_type = value.get("recommendation_type") or value.get("type") or "未分类"
        state = _status(value.get("state"))
        facts.append(f"建议 {recommendation_id}：类型为{recommendation_type}，状态为{state}。")
    return facts


def _recommendation_audit_facts(observation: dict[str, Any]) -> list[str]:
    items = _list(observation.get("items"))
    facts = [f"该建议共有 {len(items)} 条状态审计记录。"]
    if items:
        latest = _dict(items[-1])
        action = latest.get("action") or latest.get("event") or "未记录动作"
        facts.append(f"最近一条记录为 {action}，只读展示，不会修改建议状态。")
    return facts


def _fallback_facts(observation: dict[str, Any]) -> list[str]:
    if not observation:
        return []
    return ["已取得可核实的业务信息，详细数据可在对应管理页面查看。"]
