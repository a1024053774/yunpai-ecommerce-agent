# 需求预测业务展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用面向经营人员的预测摘要和需求把握水平替代技术诊断卡片。

**Architecture:** 管理后台继续从既有预测和需求事实接口读取数据，不新增 API。前端将日需求事实数和预测窗口拼成摘要，并把 `p50`、`p80`、`p95` 的表头改为中文业务含义；静态页面测试防止技术诊断字段重新出现在主界面。

**Tech Stack:** FastAPI 静态管理后台、原生 JavaScript、pytest。

## Global Constraints

- 不改变预测算法、回测、持久化、库存或采购行为。
- 不新增依赖、Schema 或 API。
- 预测主界面不得显示运行编号、数据水位、原始 WAPE 或独立“最新预测”卡片。
- 页面保留 `p50`、`p80`、`p95` 对应的原始数据值，但用中文解释其业务含义。

---

### Task 1: 预测摘要与需求区间展示

**Files:**
- Modify: `docs/admin-console.html:356-367,2166-2194`
- Modify: `tests/test_admin_console.py:test_admin_console_forecasting_view_is_prediction_only`

**Interfaces:**
- Consumes: `GET /v1/forecasting/skus/{store_id}/{sku_id}/latest` 返回 `forecast_horizon`、`forecast_points` 和需求事实接口返回 `facts`。
- Produces: `forecastMethod` 容器中的业务摘要，以及未来需求表的中文列名。

- [ ] **Step 1: Write the failing test**

```python
def test_admin_console_forecasting_view_is_prediction_only(tmp_path) -> None:
    ...
    assert 'id="forecastSummary"' not in page.text
    assert "基于 ${demand.facts.length} 天历史销售数据，预测未来 ${forecast.forecast_horizon} 天需求" in page.text
    assert "50% 把握需求（P50）" in page.text
    assert "80% 把握需求（P80）" in page.text
    assert "95% 把握需求（P95）" in page.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_forecasting_view_is_prediction_only`

Expected: FAIL because the existing page still includes `forecastSummary` and only uses P50/P80/P95 table headings.

- [ ] **Step 3: Write minimal implementation**

```javascript
$('forecastMethod').innerHTML = `<div class="stack">
  <div><strong>${escapeHtml(forecastDisplayName(forecast.champion_model))}</strong></div>
  <div>基于 ${escapeHtml(demand.facts.length)} 天历史销售数据，预测未来 ${escapeHtml(forecast.forecast_horizon)} 天需求</div>
  ...
</div>`;
```

Remove the `forecastSummary` article and all writes to that element. Replace the future table headings with `50% 把握需求（P50）`、`80% 把握需求（P80）`、`95% 把握需求（P95）`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_forecasting_view_is_prediction_only`

Expected: PASS.

- [ ] **Step 5: Run focused regression and source checks**

Run: `python -m pytest -q tests/test_admin_console.py tests/test_forecast_three_year_demo.py tests/test_forecasting_workbench_api.py; python -m compileall -q src; git diff --check`

Expected: all selected tests pass, compilation produces no error, and Git reports no whitespace errors.

- [ ] **Step 6: Commit**

```bash
git add docs/admin-console.html tests/test_admin_console.py docs/superpowers/plans/2026-08-09-forecast-business-display.md
git commit -m "fix(admin): clarify forecast business summary"
```

## Self-Review

- Spec coverage: Task 1 implements the removal of the technical card, the historical-data/future-window summary, and Chinese `p50`/`p80`/`p95` meanings. The already committed design document covers the future SKU unit and procurement rounding integration without introducing it here.
- Placeholder scan: no incomplete implementation steps or undefined interfaces.
- Type consistency: all referenced properties are already supplied by the existing page requests.
