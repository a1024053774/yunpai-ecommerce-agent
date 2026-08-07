# M6-R Complete Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the complete M6-R demand forecasting and inventory planning route from the workbench, while keeping procurement and payment actions out of scope.

**Architecture:** Add a versioned demand-fact layer over existing order and inventory services, a pure-Python model/backtest engine, and persisted forecasting/planning evidence. Expose the same read/query core through authenticated APIs, two read-only Agent tools, and the existing admin console. Forecast output and inventory plans remain auditable and deterministic; no supplier or commerce-order action is performed.

**Tech Stack:** Python 3.12, SQLite migrations v29/v30, Pydantic v2, FastAPI, existing ToolRegistry, pytest. No new dependencies.

## Global Constraints

- `demand-v1` includes paid or partially-refunded, non-cancelled order-line quantity only.
- All business dates use `Asia/Shanghai`; no server-local date assumptions.
- Existing schema reservations 29 and 30 are used exactly once with `_apply_v29` and `_apply_v30`.
- Facts are tenant/store/SKU scoped; inventory is tenant/store/warehouse/SKU scoped.
- Replays are idempotent by source watermark and payload hash; corrections append a new fact version.
- Missing, stockout, and unknown evidence are explicit and never silently converted to zero demand.
- Forecasts use time-ordered data only; no random split or future leakage.
- P50/P80/P95 must be monotonic by validation, not post-hoc sorting.
- M6-R never creates a purchase order, changes inventory, pays a supplier, or calls an external connector.

---

### Task 1: Schema 29/30 And Migration Evidence

**Files:**
- Modify: `src/ecommerce_agent/database.py`
- Test: `tests/test_migrations.py`
- Modify: `CONTRIBUTING.md` only if the reserved-row implementation status must be recorded by the repository owner; do not change other coordination rows.

**Interfaces:**
- Produces tables `demand_daily_facts`, `forecast_policies`, `forecast_runs`, `forecast_backtests`, `forecast_points`, `forecast_anomalies`, `inventory_planning_policies`, and `inventory_plans`.
- Produces indexes for tenant/store/SKU/date and tenant/store/warehouse/SKU lookup.

- [ ] Write migration tests for fresh initialization, v27→v29→v30 upgrade, nullable/default compatibility, schema migration idempotency, and tenant-scoped indexes.
- [ ] Run the migration tests red on the current schema 27 database.
- [ ] Implement `_apply_v29` and `_apply_v30` in version order, retaining existing migrations and updating `SCHEMA_VERSION` to 30.
- [ ] Run migration tests green twice with `initialize()` repeated.
- [ ] Commit only database and migration tests as `feat(forecasting): add schema 29 and 30 storage`.

### Task 2: Versioned Demand Fact Builder

**Files:**
- Create: `src/ecommerce_agent/business/demand_facts.py`
- Modify: `src/ecommerce_agent/business/service.py`
- Modify: `src/ecommerce_agent/business/__init__.py`
- Test: `tests/test_demand_facts.py`

**Interfaces:**
- `DemandPolicy` exposes `policy_version`, `timezone`, payment/order status rules, lookback days, and missing-date policy.
- `DemandFactService.rebuild(tenant_id, store_id, sku_id, start_date, end_date, mode)` returns fact versions, source watermark, quality and evidence.
- `DemandFactService.list_facts(tenant_id, store_id, sku_id, start_date, end_date)` returns only the requested tenant facts in date order.

- [ ] Write red tests for paid/cancelled filtering, Shanghai midnight boundaries, gross/eligible/order count/sales amount aggregation, missing dates versus source gaps, stockout `true/false/unknown`, source watermark, idempotent replay and correction append.
- [ ] Implement a read repository over `OrderService`-compatible order facts and `InventoryService` snapshots; do not infer SKU refunds from order-level refund amounts.
- [ ] Persist canonical payload hashes and enforce duplicate replay idempotency inside a write transaction.
- [ ] Add full rebuild and bounded lookback rebuild paths; expose `fact_version`, `demand_policy_version`, `source_watermark`, and `quality_level`.
- [ ] Run red/green tests and commit as `feat(forecasting): persist versioned demand daily facts`.

### Task 3: Forecast Models And Rolling Backtest

**Files:**
- Modify: `src/ecommerce_agent/business/forecasting.py`
- Create: `src/ecommerce_agent/business/forecast_models.py` if the model code no longer fits the existing service module.
- Test: `tests/test_forecast_models.py`

**Interfaces:**
- `ForecastModel.predict(history, horizon_days) -> list[Decimal]` remains deterministic and side-effect free.
- Candidate registry contains `last_value`, `7_day_seasonal_naive`, `rolling_mean`, `ewma`, `croston`, and `tsb`.
- `RollingBacktest.run(series, candidates, horizon, windows) -> BacktestReport` stores each origin, actual, forecast, error, and failure reason.
- `ChampionSelector.select(report, baseline_name, improvement_threshold) -> ChampionDecision` records ranking and reason.

- [ ] Write red tests for stable, trend, weekly-seasonal, intermittent, zero-heavy and cold-start series; assert no future dates enter training.
- [ ] Implement pure-Python rolling mean, EWMA, Croston and TSB without adding dependencies.
- [ ] Implement WAPE, Bias, sMAPE, RMSE and zero-denominator handling as explicit comparable/unavailable results.
- [ ] Validate interval monotonicity before persistence; invalid model output fails that candidate/run rather than sorting values.
- [ ] Make failed candidates non-blocking and fall back to baseline when improvement threshold is not met.
- [ ] Persist `forecast_runs`, `forecast_backtests`, `forecast_points` and `forecast_anomalies` with model version, data hash, policy evidence and champion reason.
- [ ] Run red/green tests and commit as `feat(forecasting): add rolling backtest and champion selection`.

### Task 4: Inventory Planning Policies And Plans

**Files:**
- Create: `src/ecommerce_agent/business/inventory_planning.py`
- Modify: `src/ecommerce_agent/business/service.py`
- Modify: `src/ecommerce_agent/business/__init__.py`
- Test: `tests/test_inventory_planning.py`

**Interfaces:**
- `InventoryPlanningPolicy` validates lead time, review period, service level, safety stock, MOQ, order multiple and maximum stock days.
- `InventoryPlanningService.upsert_policy()` stores tenant/store/SKU policy with optional warehouse override.
- `InventoryPlanningService.create_plan()` consumes a forecast run and one inventory snapshot and returns an immutable plan.
- `InventoryPlanningService.get_plan()` returns the stored plan and all calculation evidence.

- [ ] Write red tests for policy precedence, `available = on_hand - reserved`, lead/review quantile demand, minimum safety stock, MOQ, order multiple, maximum stock days, stockout date, risk levels, deterministic repeatability and no multi-warehouse demand duplication.
- [ ] Implement the fixed calculation order and preserve every intermediate value and rounding decision in JSON evidence.
- [ ] Persist plans as immutable snapshots keyed by tenant/store/SKU/forecast run/policy version; a new inventory snapshot creates a new plan rather than mutating history.
- [ ] Keep warehouse as supply location only; demand remains store+SKU scope.
- [ ] Run red/green tests and commit as `feat(forecasting): persist deterministic inventory plans`.

### Task 5: Forecasting API And Read-Only Agent Tools

**Files:**
- Modify: `src/ecommerce_agent/forecasting_api.py`
- Modify: `src/ecommerce_agent/api.py`
- Modify: `src/ecommerce_agent/business/service.py`
- Test: `tests/test_forecasting_api.py`, `tests/test_tools.py`

**Interfaces:**
- Implement the workbench endpoints: demand rebuild/query, forecast run/detail, latest SKU forecast, backtest, policy update, inventory plan and risk list.
- Register `get_demand_forecast` and `get_inventory_plan` as read-only tools with tenant/store scope policies.
- All errors use stable codes and all writes create audit events; no endpoint creates procurement actions.

- [ ] Write red tests for admin auth, 422 validation, tenant isolation, stale/cold-start degradation, stable error codes, policy precedence, read-only tool behavior and audit evidence.
- [ ] Implement thin routers that call domain services; keep calculations out of route handlers.
- [ ] Add forecast run identifiers and data/policy evidence to all responses.
- [ ] Run API/tool tests and the existing API/tool regression suite; commit as `feat(forecasting): expose forecast and inventory planning APIs`.

### Task 6: Admin Forecasting Views

**Files:**
- Modify: `docs/admin-console.html`
- Test: `tests/test_forecasting_admin.py` or existing admin browser/route tests.

**Interfaces:**
- Add demand history, forecast interval, inventory line, stockout date, recommendation, backtest metrics and quality explanation to the existing admin console.
- Reuse current admin authentication and responsive layout; no purchase execution controls.

- [ ] Write route/HTML contract tests for data loading and draft-only labels.
- [ ] Implement compact tables and detail panels using existing styles and escaping helpers.
- [ ] Verify desktop and mobile layouts with the existing local admin route; capture evidence for no overflow/overlap.
- [ ] Commit as `feat(forecasting): add admin forecast and inventory views`.

### Task 7: Synthetic Eval, Counterexamples, And Delivery

**Files:**
- Create: `evals/forecasting/fixtures/*.json`
- Create: `evals/forecasting/run_forecast_eval.py`
- Create: `tests/test_forecasting_eval.py`
- Modify: `docs/superpowers/specs/2026-08-07-forecast-order-draft-design.md` only if the final API evidence changes.

**Interfaces:**
- Cover stable, rising, falling, weekly, intermittent, zero-heavy, promotion spike, stockout truncation, missing data and cold start fixtures.
- Keep ground truth separate from production forecast inputs.

- [ ] Write red eval gates for rolling-origin use, baseline fallback, WAPE/Bias comparability, interval coverage and no ground-truth leakage.
- [ ] Implement the synthetic runner and machine-readable gate report.
- [ ] Temporarily break time-order slicing, interval monotonicity and stockout handling; verify each gate fails, restore and rerun.
- [ ] Run forecasting, API, admin, eval, migration, related regression and full-suite commands with proxy isolation; record actual results.
- [ ] Update PR #7 description with scope, test counts, counterexamples and known limitations; do not merge automatically.
