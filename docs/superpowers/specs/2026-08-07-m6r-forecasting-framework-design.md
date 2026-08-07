# M6-R Forecasting Framework Design

## Purpose

This change establishes the smallest runnable forecasting slice for the M6-R
需求预测与智能补货 route. It is a framework handoff for the later WP1-WP5
implementation, not the complete forecasting product.

The runnable path is:

```text
commerce_orders + commerce_order_lines
        -> demand-v1 daily SKU series
        -> deterministic baseline forecast
        -> inventory planning calculation
        -> draft-only preview response
```

## Scope

The framework provides:

- `demand-v1` order inclusion rules and `Asia/Shanghai` business dates;
- tenant/store/SKU-scoped demand aggregation with missing-date evidence;
- a model protocol and registry with `last_value` and
  `7_day_seasonal_naive` baselines;
- deterministic 7/14/30-day forecast windows, defaulting to 7 days;
- draft replenishment calculation using inventory balance, lead time, review
  period, service-level quantile, safety stock, minimum order quantity and
  order multiple;
- a read-only `POST /v1/forecasting/preview` API protected by existing admin
  authentication and tenant isolation.

The response explicitly reports `status=draft`, `persisted=false`, and
`external_order_created=false`. The preview writes only an audit event; it
does not create forecast tables, orders, purchase requests or connector
actions.

## Demand Contract

The primary target is `fulfillable_demand_units`:

- include order-line quantity when the order is not cancelled and payment
  status is `paid` or `partially_refunded`;
- exclude cancelled and unpaid orders;
- group by `tenant_id + store_id + sku_id + business_date`;
- convert order timestamps to the fixed UTC+8 `Asia/Shanghai` business date;
- fill dates inside the observed range with zero eligible units;
- retain gross units, eligible units, source watermark, policy version and
  missing-date evidence in the in-memory series;
- do not infer SKU-level returned quantity from order-level refund amounts.

The framework requires 14 observed calendar days for the seasonal baseline.
Insufficient history returns `forecast_insufficient_history` rather than a
fabricated forecast.

## Forecast Contract

`ForecastRequest` requires `store_id`, `warehouse_id` and `sku_id`, and accepts
7, 14 or 30 forecast days. It also accepts history length, supplier lead time,
review period, service level (`p50`, `p80`, `p95`), safety-stock days, minimum
order quantity and order multiple.

The model protocol is replaceable without changing the service or API. The
initial registry contains:

- `last_value`: repeats the latest eligible daily demand;
- `7_day_seasonal_naive`: repeats the latest complete seven-day pattern and
  is the default preview model.

The initial interval is deliberately marked uncalibrated. `p50` is the model
point forecast; `p80` adds one deterministic recent seasonal error scale; and
`p95` adds two. The service enforces `p50 <= p80 <= p95`. WP2 will replace this
heuristic with rolling-origin backtest quantiles and champion selection.

## Inventory Planning Contract

The planning horizon is `lead_time_days + review_period_days`, or the requested
forecast horizon when it is longer. The calculation is code-owned:

```text
available = max(0, on_hand - reserved)
target_demand = sum(selected service-level forecast over planning horizon)
safety_stock = mean forecast demand * safety_stock_days
raw_order_qty = max(0, target_demand + safety_stock - available - inbound)
```

The raw quantity is first raised to the minimum order quantity when positive,
then rounded up to the configured order multiple. Every intermediate value is
returned in the response. A warehouse is required for the inventory lookup;
the demand series remains store + SKU level because the current order facts do
not contain a reliable fulfillment warehouse.

## Error and Safety Boundaries

- Missing or isolated order history returns `forecast_insufficient_history`.
- Missing tenant/store/warehouse/SKU inventory returns
  `inventory_balance_not_found` without revealing another tenant's data.
- Invalid request values are rejected by Pydantic with HTTP 422.
- Domain data errors return HTTP 409 through the forecasting router.
- No model output can create an order or mutate inventory.
- No Schema 29 or Schema 30 migration is included in this framework.

## Deferred M6-R Work

The following remain for the formally assigned WP1-WP5 owners:

- `demand_daily_facts` persistence, watermarks, rebuilds and backfills;
- stockout/censored-demand evidence and quality gates;
- rolling-origin backtests, WAPE/Bias/sMAPE/RMSE, pinball loss and champion
  fallback;
- Croston/TSB and additional demand-type handling;
- Schema 29/30 tables, historical plans and forecast runs;
- read-only Agent tools, admin screens, synthetic Eval and shadow mode.
