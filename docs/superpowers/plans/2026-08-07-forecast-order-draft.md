# Forecast Order Draft Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a reusable, draft-only replenishment forecast order from the existing sales-demand and inventory-preview path.

**Architecture:** Keep calculations in `ForecastingService` and add a typed `ForecastOrderDraft` output object. The existing authenticated preview endpoint returns that object without persistence, commerce-order creation, inventory writes, or supplier integration.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLite fixtures, pytest.

## Global Constraints

- Only paid or partially-refunded, non-cancelled order lines form `demand-v1`.
- Business dates use `Asia/Shanghai` fixed UTC+8 conversion.
- Supported forecast horizons are exactly 7, 14, or 30 days.
- `P50 <= P80 <= P95` must remain true for every returned day.
- No schema migration, new table, purchase order, inventory write, supplier call, or external order creation.
- The returned order is always `status="draft"`, `persisted=false`, and `external_order_created=false`.
- Tenant, store, warehouse, and SKU boundaries remain enforced by existing services.

---

### Task 1: Add The Typed Forecast-Order Draft

**Files:**
- Modify: `src/ecommerce_agent/business/forecasting.py`
- Modify: `src/ecommerce_agent/business/__init__.py`
- Test: `tests/test_forecasting_framework.py`

**Interfaces:**
- Consumes: `ForecastRequest`, `DemandSeries`, selected forecast points, and an inventory balance.
- Produces: `ForecastOrderDraft` with identity, recommendation, risk dates, service level, and forecast evidence.

- [ ] **Step 1: Write the failing domain-output test**

```python
def test_preview_returns_reusable_forecast_order_draft(tmp_path) -> None:
    service, tenant_id = _service_with_history_and_balance(tmp_path)
    result = service.preview(tenant_id, ForecastRequest(..., lead_time_days=3))

    assert result["forecast_order"]["kind"] == "forecast_replenishment"
    assert result["forecast_order"]["status"] == "draft"
    assert result["forecast_order"]["recommended_quantity"] == result["replenishment"]["recommended_order_qty"]
    assert result["forecast_order"]["recommended_arrival_date"] == "2026-08-17"
    assert result["forecast_order"]["expected_stockout_date"] == "2026-08-16"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `D:\yunpai\.venv\Scripts\python.exe -m pytest -q tests/test_forecasting_framework.py::test_preview_returns_reusable_forecast_order_draft`

Expected: FAIL because the `forecast_order` key does not exist.

- [ ] **Step 3: Add the minimal typed object and calculation helpers**

```python
class ForecastOrderDraft(BaseModel):
    kind: Literal["forecast_replenishment"] = "forecast_replenishment"
    status: Literal["draft"] = "draft"
    persisted: Literal[False] = False
    external_order_created: Literal[False] = False
    store_id: str
    warehouse_id: str
    sku_id: str
    recommended_quantity: str
    expected_stockout_date: str | None
    recommended_arrival_date: str
    service_level: ServiceLevel
    forecast_basis: dict[str, str | None]
```

Build it in `ForecastingService.preview()` from the selected quantile, last business date, source watermark, policy version, and replenishment result. Compute stockout by starting at `available + inbound`, subtracting selected-quantile daily demand in order, and returning the first date where supply becomes negative.

- [ ] **Step 4: Run the targeted test to verify it passes**

Run: `D:\yunpai\.venv\Scripts\python.exe -m pytest -q tests/test_forecasting_framework.py::test_preview_returns_reusable_forecast_order_draft`

Expected: PASS.

- [ ] **Step 5: Commit the domain change**

```powershell
git add src/ecommerce_agent/business/forecasting.py src/ecommerce_agent/business/__init__.py tests/test_forecasting_framework.py
git commit -m "feat(forecasting): expose forecast replenishment draft"
```

### Task 2: Verify API Contract And No-Side-Effect Boundary

**Files:**
- Modify: `tests/test_forecasting_framework.py`
- Modify only if necessary: `src/ecommerce_agent/forecasting_api.py`

**Interfaces:**
- Consumes: `POST /v1/forecasting/preview`, existing admin authentication, and `ForecastOrderDraft` returned by `ForecastingService.preview()`.
- Produces: a stable draft object in the HTTP response and a forecast-preview audit event only.

- [ ] **Step 1: Write the failing API contract test**

```python
response = client.post("/v1/forecasting/preview", headers=admin_headers, json=payload)
assert response.status_code == 200
assert response.json()["forecast_order"]["status"] == "draft"
assert response.json()["forecast_order"]["persisted"] is False
assert response.json()["forecast_order"]["external_order_created"] is False
assert not _table_exists(db, "inventory_plans")
assert _commerce_order_count(db) == before_order_count
```

- [ ] **Step 2: Run the API test to verify it fails**

Run: `D:\yunpai\.venv\Scripts\python.exe -m pytest -q tests/test_forecasting_framework.py::test_preview_api_returns_draft_and_audit_only`

Expected: FAIL until the forecast-order object is returned by the service.

- [ ] **Step 3: Keep the router thin**

Use the existing router and admin dependency. Do not add write endpoints; the existing audit event is the only side effect.

- [ ] **Step 4: Run the API test to verify it passes**

Run: `D:\yunpai\.venv\Scripts\python.exe -m pytest -q tests/test_forecasting_framework.py::test_preview_api_returns_draft_and_audit_only`

Expected: PASS.

- [ ] **Step 5: Commit the API evidence**

```powershell
git add tests/test_forecasting_framework.py src/ecommerce_agent/forecasting_api.py
git commit -m "test(forecasting): lock draft-only preview contract"
```

### Task 3: Run Regression, Counterexample, And Deliver

**Files:**
- Test: `tests/test_forecasting_framework.py`
- Test: `tests/test_ops_assistant.py`
- Test: `tests/test_catalog_orders_metrics.py`
- Test: `tests/test_order_handoff_visibility.py`

**Interfaces:**
- Consumes: the completed forecasting service and preview API.
- Produces: fresh test evidence and an updated Draft PR without changing its draft-only safety boundary.

- [ ] **Step 1: Run forecasting and adjacent regressions**

```powershell
D:\yunpai\.venv\Scripts\python.exe -m pytest -q tests/test_forecasting_framework.py tests/test_ops_assistant.py tests/test_catalog_orders_metrics.py tests/test_order_handoff_visibility.py
D:\yunpai\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 2: Run a focused counterexample**

Temporarily change the draft object's `external_order_created` to `True`; the API draft-only test must fail. Restore the code and rerun the forecasting suite.

- [ ] **Step 3: Push and update the existing Draft PR**

```powershell
git push origin codex/m6r-forecasting-framework
```

Update PR #7 with actual test counts and state that this remains a forecast-order draft, not a real procurement workflow.
