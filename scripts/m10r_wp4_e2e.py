"""M10-R WP4 端到端场景复跑脚本（HTTP 全链，无第三方依赖）。

覆盖：正式三层利润 / 缺失不补零 / demo 隔离与标签 / 双算对账 /
订购单 Gate 与状态机 / 写屏障（状态推进不产生利润副作用）。

用法：
  python scripts/m10r_wp4_e2e.py --base-url http://127.0.0.1:8081 \
      --admin-id admin-test --admin-key <key> --out docs/works/18-feature-m10r-wp4/e2e-scenario-20260821.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


STORE = "e2e-store"
MISSING_STORE = "e2e-missing"
POLICY_VERSION = "v-e2e"
PERIOD = "2026-08"
ORDER = "E2E-1"


def call(
    base_url: str,
    admin_id: str,
    admin_key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = f"{base_url}{path}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Admin-Id": admin_id,
            "X-Admin-Key": admin_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            parsed = json.loads(payload) if payload else None
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            parsed = payload.decode("utf-8", errors="replace") if payload else None
        return exc.code, parsed


def check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {name} — {detail}")


def post_entry(
    base: str,
    admin_id: str,
    admin_key: str,
    *,
    store: str,
    category: str,
    amount: str,
    scope: str,
    source_kind: str,
    entry_key: str,
    order_id: str | None = None,
) -> tuple[int, Any]:
    body = {
        "store_id": store,
        "period": PERIOD,
        "category": category,
        "scope": scope,
        "amount": amount,
        "source_kind": source_kind,
        "entry_key": entry_key,
    }
    if order_id:
        body["order_id"] = order_id
    return call(base, admin_id, admin_key, "POST", "/v1/profit/ledger/entries", body)


def seed_delivered_order(
    base: str,
    admin_id: str,
    admin_key: str,
    store: str,
    order_id: str,
) -> tuple[int, Any]:
    body = {
        "connector_id": "e2e-conn",
        "store_id": store,
        "order_id": order_id,
        "order_status": "delivered",
        "payment_status": "paid",
        "currency": "CNY",
        "total_amount": "1000.00",
        "placed_at": "2026-08-01T00:00:00+00:00",
        "lines": [
            {
                "line_id": f"{order_id}-l1",
                "sku_id": "E2E-SKU",
                "title": "e2e product",
                "quantity": 1,
                "unit_price": "1000.00",
            }
        ],
        "source_updated_at": "2026-08-01T00:00:00+00:00",
    }
    return call(base, admin_id, admin_key, "POST", "/v1/orders", body)


def run(
    base_url: str,
    admin_id: str,
    admin_key: str,
) -> list[dict[str, Any]]:
    global STORE, MISSING_STORE, POLICY_VERSION
    checks: list[dict[str, Any]] = []
    base = base_url.rstrip("/")
    run_tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    STORE = f"e2e-{run_tag}"
    MISSING_STORE = f"e2e-miss-{run_tag}"
    POLICY_VERSION = f"v-e2e-{run_tag}"

    # 1. 政策
    status, policy = call(
        base, admin_id, admin_key, "POST", "/v1/profit/policies",
        {"policy_version": POLICY_VERSION},
    )
    if status == 409 and isinstance(policy, dict) and "version_conflict" in str(policy):
        status = 200
    check(checks, "登记利润政策", status == 200, f"status={status} policy={policy}")

    # 2. 正式全量
    seed_delivered_order(base, admin_id, admin_key, STORE, ORDER)
    entries = [
        ("signed_receipt_revenue", "1000.00", "actual", ORDER),
        ("purchase_cost", "-300.00", "manual", None),
        ("direct_product_cost", "-100.00", "manual", None),
        ("platform_fee", "-80.00", "manual", None),
        ("advertising_cost", "-50.00", "manual", None),
        ("forward_logistics_cost", "-30.00", "manual", None),
        ("transport_insurance", "-10.00", "manual", None),
        ("tax_cost", "-20.00", "manual", None),
    ]
    for index, (category, amount, source, order) in enumerate(entries):
        status, _ = post_entry(
            base, admin_id, admin_key,
            store=STORE, category=category, amount=amount, scope="formal",
            source_kind=source, entry_key=f"{STORE}-e2e-{index}", order_id=order,
        )
        if status not in (200, 409):
            check(checks, f"入账 {category}", False, f"status={status}")
            return checks
    status, projection = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={STORE}&period={PERIOD}&scope=formal",
    )
    sales = projection["sales"]
    operating = projection["operating"]
    final = projection["final"]
    ok = (
        status == 200
        and sales["status"] == "available" and sales["amount"] == "600.00"
        and operating["status"] == "available" and operating["amount"] == "430.00"
        and final["status"] == "available" and final["amount"] == "410.00"
    )
    check(
        checks, "正式三层利润全可用（600/430/410）",
        ok, f"status={status} {sales['amount']}/{operating['amount']}/{final['amount']}",
    )

    # 3. 缺失不补零
    seed_delivered_order(base, admin_id, admin_key, MISSING_STORE, "E2E-MISS")
    for index, (category, amount, source, order) in enumerate(
        [
            ("signed_receipt_revenue", "1000.00", "actual", "E2E-MISS"),
            ("purchase_cost", "-300.00", "manual", None),
            ("direct_product_cost", "-100.00", "manual", None),
        ]
    ):
        post_entry(
            base, admin_id, admin_key,
            store=MISSING_STORE, category=category, amount=amount, scope="formal",
            source_kind=source, entry_key=f"{MISSING_STORE}-e2e-miss-{index}", order_id=order,
        )
    status, missing = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={MISSING_STORE}&period={PERIOD}&scope=formal",
    )
    ok = (
        status == 200
        and missing["operating"]["status"] == "missing"
        and missing["operating"]["amount"] is None
        and "platform_fee" in missing["operating"]["missing_fields"]
    )
    check(
        checks, "缺失费用不补零（经营层 missing + 金额 null）",
        ok,
        f"status={status} operating={missing['operating']['status']} "
        f"amount={missing['operating']['amount']}",
    )

    # 4. demo 隔离与标签
    for index, (category, amount) in enumerate(
        [
            ("signed_receipt_revenue", "2000.00"),
            ("purchase_cost", "-500.00"),
            ("direct_product_cost", "-200.00"),
        ]
    ):
        post_entry(
            base, admin_id, admin_key,
            store=STORE, category=category, amount=amount, scope="demo",
            source_kind="demo", entry_key=f"{STORE}-e2e-demo-{index}",
            order_id="E2E-DEMO" if category == "signed_receipt_revenue" else None,
        )
    _, demo = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={STORE}&period={PERIOD}&scope=demo",
    )
    _, formal_after = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={STORE}&period={PERIOD}&scope=formal",
    )
    ok = (
        demo["sales"]["status"] == "available"
        and demo["sales"]["amount"] == "1300.00"
        and demo["sales"]["label"].startswith("销售利润试算")
        and formal_after["final"]["amount"] == "410.00"
    )
    check(
        checks, "demo 隔离与标签（试算 1300，formal 不受影响）",
        ok,
        f"demo_sales={demo['sales']['amount']} formal_final={formal_after['final']['amount']}",
    )

    # 5. 双算对账
    post_entry(
        base, admin_id, admin_key,
        store=STORE, category="refund_offset", amount="-50.00", scope="formal",
        source_kind="actual", entry_key=f"{STORE}-e2e-refund-1", order_id=ORDER,
    )
    post_entry(
        base, admin_id, admin_key,
        store=STORE, category="refund_offset", amount="-50.00", scope="formal",
        source_kind="actual", entry_key=f"{STORE}-e2e-refund-2", order_id=ORDER,
    )
    status, reconcile = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/reconciliation?store_id={STORE}&period={PERIOD}&scope=formal",
    )
    ok = (
        status == 200
        and reconcile["double_count_ok"] is False
        and any(
            issue["code"] == "duplicate_order_category_entry"
            for issue in reconcile["issues"]
        )
    )
    check(checks, "重复退款被对账检出", ok, f"status={status} issues={len(reconcile['issues'])}")
    _, projection_after_refund = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={STORE}&period={PERIOD}&scope=formal",
    )
    expected_final = projection_after_refund["final"]["amount"]

    # 6. 订购单：formal Gate 阻断 + demo 放行 + 状态机 + 写屏障
    status, blocked = call(
        base, admin_id, admin_key, "POST", "/v1/ordering/drafts",
        {
            "store_id": STORE,
            "sku_id": "E2E-SKU",
            "recommended_qty": 10,
            "source_summary": "e2e gate block",
        },
    )
    check(checks, "订购单 formal Gate 阻断（缺证据）", status == 409, f"status={status} detail={blocked}")
    status, demo_draft = call(
        base, admin_id, admin_key, "POST", "/v1/ordering/drafts",
        {
            "store_id": STORE,
            "sku_id": "E2E-SKU",
            "recommended_qty": 10,
            "source_summary": "e2e demo draft",
            "mode": "demo",
        },
    )
    draft_id = demo_draft["order_draft_id"] if status == 200 else ""
    check(
        checks, "订购单 demo 放行且带未发送标签",
        status == 200 and demo_draft["unsent_label"] == "未发送（演示参数）",
        f"status={status} label={demo_draft.get('unsent_label')}",
    )
    status, submitted = call(
        base, admin_id, admin_key, "POST",
        f"/v1/ordering/drafts/{draft_id}/submit?store_id={STORE}",
    )
    status, confirmed = call(
        base, admin_id, admin_key, "POST",
        f"/v1/ordering/drafts/{draft_id}/confirm?store_id={STORE}",
        {"version": 1, "confirmed_qty": 10},
    )
    check(
        checks, "订购单 draft→awaiting→confirmed（版本 2）",
        submitted["status"] == "awaiting_confirmation"
        and confirmed["status"] == "confirmed"
        and confirmed["version"] == 2,
        f"submit={submitted.get('status')} confirm={confirmed.get('status')} v={confirmed.get('version')}",
    )
    _, profit_after_order = call(
        base, admin_id, admin_key, "GET",
        f"/v1/profit/projection?store_id={STORE}&period={PERIOD}&scope=formal",
    )
    check(
        checks, "写屏障：订购单推进不改变利润投影",
        profit_after_order["final"]["amount"] == expected_final,
        f"final={profit_after_order['final']['amount']} expected={expected_final}",
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--admin-id", default="admin-test")
    parser.add_argument("--admin-key", default="test-admin-key-123456")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    checks = run(args.base_url, args.admin_id, args.admin_key)
    summary = {
        "passed": sum(1 for item in checks if item["passed"]),
        "failed": sum(1 for item in checks if not item["passed"]),
    }
    document = {
        "title": "M10-R WP4 端到端场景证据",
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "checks": checks,
        "summary": summary,
    }
    with open(args.out, "w", encoding="utf-8") as file_handle:
        json.dump(document, file_handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
