from __future__ import annotations

import re
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
}


STATUS_LABELS: dict[str, str] = {
    "active": "正常",
    "inactive": "停用",
    "available": "可用",
    "no_data": "暂无数据",
    "traceable": "来源可追溯",
    "source_id_missing": "来源信息不完整",
    "resolved": "已找到唯一结果",
    "ambiguous": "找到多个可能结果",
    "no_match": "未找到结果",
    "paid": "已支付",
    "partially_refunded": "部分退款",
    "refunded": "已退款",
    "pending": "待处理",
    "processing": "处理中",
    "shipped": "已发货",
    "delivered": "已送达",
    "completed": "已完成",
    "canceled": "已取消",
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "healthy": "正常",
    "stockout": "已经缺货",
    "stockout_risk": "临近缺货",
    "replenishment_due": "需要补货",
    "slow_moving": "销售偏慢",
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
    }


def observation_data_status(
    tool_name: str, observation: dict[str, Any]
) -> str:
    if tool_name == "get_business_metric":
        quality = str(observation.get("quality") or "")
        evidence_count = _number(observation.get("evidence_count"))
        if quality == "no_data" or evidence_count == 0:
            return "no_data"
    if tool_name == "get_inventory_risk" and not _list(observation.get("risks")):
        return "no_data"
    return "success"


def critical_fact_values(product_view: dict[str, Any]) -> list[str]:
    values: list[str] = []
    identifier_pattern = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z][A-Za-z0-9-]*\b")
    number_pattern = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?")
    for fact in product_view.get("已核实信息") or []:
        text = str(fact)
        for match in [*identifier_pattern.findall(text), *number_pattern.findall(text)]:
            if match not in values:
                values.append(match)
    return values


def answer_preserves_critical_values(
    answer: str, results: list[Any]
) -> bool:
    required = {
        str(value)
        for result in results
        if getattr(result, "status", None) == "success"
        for value in getattr(result, "critical_values", [])
        if str(value)
    }
    return all(value in answer for value in required)


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
    raw = str(value or "未知")
    return STATUS_LABELS.get(raw, raw)


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
            f"知识学习候选有 {_number(counts.get('pending_learning'))} 条，质检待复核有 "
            f"{_number(counts.get('pending_qa_reviews'))} 条。"
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
    findings = _list(observation.get("findings"))
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
        facts.append(
            f"商品 {risk.get('sku_id') or '未提供编号'}：{_status(risk.get('risk_code'))}，"
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
    lower = summary.get("lower_price_competitors") or summary.get("competitor_lower_price_count")
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


def _fallback_facts(observation: dict[str, Any]) -> list[str]:
    if not observation:
        return []
    return ["已取得可核实的业务信息，详细数据可在对应管理页面查看。"]
