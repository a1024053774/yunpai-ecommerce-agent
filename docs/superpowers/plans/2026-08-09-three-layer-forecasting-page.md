# Three-Layer Forecasting Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Present sales demand forecasting, inventory projection, and replenishment recommendation as one Chinese workbench without connecting new data sources.

**Architecture:** Keep the existing demand forecast API and chart unchanged. Add two read-only presentation panels after it in `view-commerce`: an inventory projection contract and a replenishment draft contract. Both use explicit empty states until their required data sources are connected, so no sample values can be mistaken for customer data.

**Tech Stack:** Static HTML, CSS, vanilla JavaScript, FastAPI static admin route, pytest.

## Global Constraints

- Do not add a data source, API request, persistence write, or external purchase action.
- All new user-facing text is Chinese.
- Do not render simulated inventory or procurement values as customer data.
- Preserve the existing sales demand forecast controls, API requests, and chart.
- Keep the three layers inside `view-commerce`; do not add a new top-level navigation entry.

---

### Task 1: Specify the Three-Layer Admin Contract

**Files:**
- Modify: `tests/test_admin_console.py:145-180, 298-322`

**Interfaces:**
- Consumes: `GET /admin` page text from the existing FastAPI application.
- Produces: structural assertions for `inventoryProjection` and `replenishmentDraft` panel identifiers and required Chinese labels.

- [ ] **Step 1: Write the failing test**

Add assertions that the commerce view contains the new sections after `demandTrendChart`, that both panels declare their data dependencies, and that no purchase endpoint/action is present:

```python
assert 'id="inventoryProjection"' in page.text
assert 'id="replenishmentDraft"' in page.text
assert page.text.index('id="demandTrendChart"') < page.text.index('id="inventoryProjection"')
assert page.text.index('id="inventoryProjection"') < page.text.index('id="replenishmentDraft"')
for label in ("库存预测", "期初库存", "计划到货", "预计退货", "期末库存", "补货建议", "采购提前期", "最低起订量", "建议补货量", "数据接入后生成补货草稿"):
    assert label in page.text
assert "purchase-order" not in page.text
assert "创建采购单" not in page.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_forecasting_view_is_prediction_only`

Expected: FAIL because `inventoryProjection` and `replenishmentDraft` do not exist.

- [ ] **Step 3: Commit the failing-test checkpoint**

```powershell
git add -- tests/test_admin_console.py
git commit -m "test(admin): require three-layer forecasting panels"
```

### Task 2: Render the Inventory Projection and Replenishment Panels

**Files:**
- Modify: `docs/admin-console.html:378-390`
- Test: `tests/test_admin_console.py:145-180, 298-322`

**Interfaces:**
- Consumes: existing `forecastStore`, `forecastSku`, `forecastMethod`, and `demandTrendChart` markup.
- Produces: `inventoryProjection` and `replenishmentDraft` read-only panels after the demand chart.

- [ ] **Step 1: Add the inventory projection panel**

After `demandTrendChart`, add a panel with id `inventoryProjection`, an explanation that inventory and supply data are pending connection, and a daily table with the columns: `日期`, `期初库存`, `锁定库存`, `计划到货`, `预计退货`, `预测销量`, `期末库存`, `缺货风险`. Its body contains only the Chinese empty state `库存、锁定量、到货计划和退货数据接入后，将生成每日库存预测。`.

- [ ] **Step 2: Add the replenishment recommendation panel**

After the inventory panel, add a panel with id `replenishmentDraft`. Display the labels `采购提前期`, `复核周期`, `安全库存`, `最低起订量`, `包装倍数`, `建议补货量`, and `预计到货日`. Render the Chinese empty state `数据接入后生成补货草稿；不会自动创建采购单。`.

- [ ] **Step 3: Keep the panels data-free**

Do not add `api(...)`, `fetch(...)`, form submission, `POST`, or a purchase/order button for either new panel. Do not alter `loadForecasting()` or the existing demand chart.

- [ ] **Step 4: Run the structural test to verify it passes**

Run: `python -m pytest -q tests/test_admin_console.py`

Expected: PASS, including the new three-layer layout assertions.

- [ ] **Step 5: Commit the implementation**

```powershell
git add -- docs/admin-console.html tests/test_admin_console.py
git commit -m "feat(admin): show three-layer forecasting workflow"
```

### Task 3: Verify the Visual Workbench Contract

**Files:**
- Modify: none
- Test: `tests/test_admin_console.py`

**Interfaces:**
- Consumes: local `/admin` page from the current application.
- Produces: desktop and mobile browser evidence that the two new panels are visible, readable, and have no overlapping controls.

- [ ] **Step 1: Run static and source checks**

Run:

```powershell
python -m compileall -q src
git diff --check
```

Expected: both commands exit with code 0.

- [ ] **Step 2: Run the focused regression suite**

Run:

```powershell
python -m pytest -q tests/test_admin_console.py tests/test_demand_facts.py tests/test_forecasting_framework.py tests/test_inventory_projection.py tests/test_forecasting_workbench_api.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Inspect the local management page**

Open `http://127.0.0.1:8095/admin`, select `商品库存`, and inspect the page at desktop and mobile widths. Confirm the order is sales demand forecast, inventory projection, then replenishment recommendation; confirm both new panels state that data is pending rather than showing fabricated values.

- [ ] **Step 4: Commit verified plan state only if a source change was necessary**

No commit is required when verification does not modify tracked files.
