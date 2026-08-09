# Forecast Real-or-Demo Source Design

## Purpose

Make the demand forecast workbench usable when a local installation has not yet received enough sales orders. A user requests a forecast for a store and SKU; the system uses eligible real paid-order history when it is sufficient, otherwise it produces a clearly labeled local three-year demonstration forecast.

## Source Resolution

Add one authenticated write endpoint for the workbench:

`POST /v1/forecasting/resolve-and-run`

The request contains `store_id`, `sku_id`, and an optional `horizon_days` of 7, 14, or 30.

1. Rebuild daily demand facts for the requested real store and SKU from paid, non-cancelled orders.
2. Count closed business-day facts with source data.
3. If the count is at least 14, create a forecast run for the requested scope and return `source_type="real"`.
4. If the count is below 14, create or reuse an isolated local demonstration scope for the current tenant and return `source_type="demo"`.

The response always identifies the effective store and SKU. The browser replaces its input values with those effective identifiers when it falls back, so displayed facts and the selected scope never disagree.

## Demonstration Data

Reuse the deterministic existing three-year generator:

- 1,095 daily virtual paid orders.
- One isolated store and one isolated SKU per tenant.
- A 30-day future forecast and 90-day historical predicted-versus-actual comparison.
- `virtual=true` and `production_claim=false` on every response and audit record.

The demo scope is separate from every user-selected store and SKU. Its order source IDs are stable, so replaying it is idempotent. If a successful demo forecast already matches its source data, it is reused rather than creating another run.

## Workbench Behavior

Rename the existing command from `刷新预测` to `生成预测`.

On success, the page displays a prominent source badge:

- `真实销售数据` for the selected real scope.
- `本地演示数据` for the isolated fallback scope, with the explanation `当前商品的真实订单历史不足，以下结果仅用于功能演示。`.

The demand facts, forecast method, trend chart, inventory projection section, and replenishment section are then rendered from the returned effective scope. The inventory and replenishment sections remain data-pending and cannot create purchase orders.

## Safety and Verification

- No fallback occurs when sufficient real history exists.
- Demo orders are never written to the requested real store or SKU.
- A real source never receives the `virtual` label.
- A demo source always receives the `virtual` label and cannot generate a procurement action.
- Tests cover real selection, insufficient-history fallback, tenant isolation, repeated demo reuse, API authorization, and the browser source badge.
