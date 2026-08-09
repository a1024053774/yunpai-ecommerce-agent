# Three-Layer Forecasting Page Design

## Purpose

Place the complete decision flow in the existing "商品与库存" workbench:

1. Sales demand forecast from paid historical orders.
2. Inventory projection after inventory, reserved quantity, dated inbound supply, returns, and forecast demand are available.
3. Replenishment recommendation after lead time, safety stock, MOQ, and order multiple are available.

This change is a presentation contract only. It must not connect a new inventory data source, create records, or create external purchase orders.

## Page Layout

The existing demand forecast section remains first. Two new sections follow it in this order:

### Inventory Projection

Show the input dependencies and the daily projection columns:

- Date
- Opening inventory
- Locked inventory
- Dated inbound supply
- Estimated returns
- Forecast sales demand
- Closing inventory
- Stockout risk

Until source data is connected, the section shows a clear Chinese empty state rather than fabricated inventory values.

### Replenishment Recommendation

Show the decision inputs and draft outputs:

- Procurement lead time
- Review period
- Safety stock
- Minimum order quantity
- Order multiple
- Suggested replenishment quantity
- Suggested arrival date

Until the inputs are connected, the section shows that a draft cannot be calculated. It has no purchase button and cannot create an order.

## Status and Safety

Every section distinguishes its data basis:

- Demand forecast: historical paid order data.
- Inventory projection: inventory and supply data pending connection.
- Replenishment recommendation: depends on an inventory projection and procurement policy; pending connection.

No sample quantities are rendered as if they were customer data. The existing demand API behavior remains unchanged.

## Verification

The administrative console test will assert the three Chinese sections, their required labels, empty states, ordering, and absence of a new purchase API/action. Browser verification will confirm that the layout is usable at desktop and mobile widths.
