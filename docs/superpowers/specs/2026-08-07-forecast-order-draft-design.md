# Forecast Order Draft Design

## Purpose

Deliver a small, reusable forecasting capability for the warehouse domain:

```text
paid, non-cancelled sales order lines
    -> daily SKU demand series
    -> future demand forecast
    -> inventory projection
    -> replenishment forecast-order draft
```

The result is a decision-support object for a human operator. It is not a
sales order, purchase order, supplier instruction, payment request, inventory
adjustment, or WMS replacement.

## Scope

The feature accepts a tenant-scoped `store_id`, `warehouse_id`, and `sku_id`.
It derives an `Asia/Shanghai` daily sales series from existing order facts,
uses deterministic 7-day seasonal naive forecasting, and combines the forecast
with the existing inventory balance.

Supported horizon values are 7, 14, and 30 days. The output contains future
daily P50/P80/P95 demand, available stock, inbound stock, a projected stockout
date when one can be calculated, and a recommended replenishment quantity.

No new database table or schema migration is introduced. The draft is returned
by the API only and is not persisted.

## Demand And Inventory Rules

- Count an order line only when its order is not `canceled` and payment status
  is `paid` or `partially_refunded`.
- Convert order timestamps to the fixed `Asia/Shanghai` business date before
  aggregation.
- Keep missing calendar dates explicit in the response; a missing source date
  is not silently described as verified zero demand.
- Compute `available = max(0, on_hand - reserved)` using the existing
  `InventoryService` balance for the requested tenant, store, warehouse, and
  SKU.
- Choose target demand from the requested P50, P80, or P95 service level across
  `lead_time_days + review_period_days`.
- Add safety stock, subtract available and inbound stock, then apply minimum
  order quantity followed by order-multiple rounding.

## Public Contract

`POST /v1/forecasting/preview` remains the only endpoint. Its response gains a
stable `forecast_order` object:

```json
{
  "forecast_order": {
    "kind": "forecast_replenishment",
    "status": "draft",
    "persisted": false,
    "external_order_created": false,
    "store_id": "store-1",
    "warehouse_id": "warehouse-1",
    "sku_id": "sku-1",
    "recommended_quantity": "120.00",
    "expected_stockout_date": "2026-08-21",
    "recommended_arrival_date": "2026-08-20",
    "service_level": "p80",
    "forecast_basis": {
      "model": "7_day_seasonal_naive",
      "data_watermark": "2026-08-14T10:00:00+00:00",
      "demand_policy_version": "demand-v1"
    }
  }
}
```

`recommended_arrival_date` is the forecast generation business date plus
`lead_time_days`. It expresses when replenishment should arrive to avoid the
projected stockout; it is not a supplier commitment.

`expected_stockout_date` is the first forecast day where projected available
plus inbound supply becomes negative. It is `null` when the selected forecast
does not exhaust available supply during the returned forecast horizon.

## Future Integration Boundary

The forecast engine owns pure demand and inventory calculations plus the
`forecast_order` output contract. A later workflow may consume that object via
an adapter, for example a repository that persists drafts or an approval system
that sends an approved request to a supplier. Those adapters are outside this
change and cannot alter the forecast calculation.

## Failure And Safety Contract

- Insufficient usable history returns `forecast_insufficient_history`.
- A tenant/store/SKU/warehouse balance that cannot be read returns
  `inventory_balance_not_found` without revealing another tenant's balance.
- The endpoint requires existing administrator authentication.
- Every response declares draft-only state. No request can make a purchase,
  create a sales order, write inventory, or call a connector.

## Test Evidence

Tests cover order eligibility, timezone conversion, missing dates, deterministic
forecast output, interval monotonicity, stock and replenishment arithmetic,
tenant/store/warehouse isolation, API authentication, and the draft-only
contract. New tests will additionally cover forecast-order identity, stockout
date, arrival date, and the guarantee that no persistence or external action is
created.
