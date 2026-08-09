# 需求预测趋势图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用单一销售需求趋势图替代历史与未来预测表格。

**Architecture:** 管理后台复用现有需求事实、回测和预测运行响应，在浏览器端合并为日期序列。原生 SVG 分别绘制实际销量、历史预测和未来 P50 预测；图例、数据点提示和空状态都由同一个渲染函数维护。

**Tech Stack:** 原生 JavaScript、SVG、FastAPI 静态管理后台、pytest。

## Global Constraints

- 不新增第三方图表依赖、API、Schema 或持久化字段。
- 实际销售需求为深青色实线，历史预测销售需求为绿色虚线，未来预测销售需求为橙色虚线。
- 未来线只使用 `forecast_points[].p50`；不展示 P80、P95 或区间带。
- 不创建库存预测、库存快照、补货订单或库存风险计算。
- 默认呈现最近 90 天历史和本次未来预测窗口；小屏幕允许图表横向滚动。

---

### Task 1: 原生 SVG 销售需求趋势图

**Files:**
- Modify: `docs/admin-console.html:80-160,365-366,2147-2188`
- Modify: `tests/test_admin_console.py:test_admin_console_forecasting_view_is_prediction_only`

**Interfaces:**
- Consumes: `demand.facts[]` 的 `business_date`、`eligible_units`；`forecast.backtests[]` 的 `actual`、`forecast`、`origin_date`；`forecast.forecast_points[]` 的 `forecast_date`、`p50`。
- Produces: `renderDemandTrendChart(demandFacts, forecast)`，写入 `#demandTrendChart` 并只渲染三种销售需求序列。

- [ ] **Step 1: Write the failing test**

```python
def test_admin_console_forecasting_view_is_prediction_only(tmp_path) -> None:
    ...
    assert 'id="demandTrendChart"' in page.text
    assert "renderDemandTrendChart" in page.text
    assert "实际销售需求" in page.text
    assert "历史预测销售需求" in page.text
    assert "未来预测销售需求（P50）" in page.text
    assert 'id="backtestRows"' not in page.text
    assert 'id="forecastRows"' not in page.text
    assert "point.p80" not in page.text
    assert "point.p95" not in page.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_forecasting_view_is_prediction_only`

Expected: FAIL because the page currently contains two table bodies and has no SVG trend chart renderer.

- [ ] **Step 3: Write minimal implementation**

```javascript
function renderDemandTrendChart(demandFacts, forecast) {
  const historical = backtestRecords(forecast);
  const future = forecast.forecast_points.map((point) => ({
    date: point.forecast_date,
    value: Number(point.p50),
  }));
  // Build one date axis and create actual, historical, and future SVG paths.
}
```

Replace the two forecast tables with a `#demandTrendChart` card, a text-and-line-style legend, SVG paths, keyboard-focusable points and a tooltip. In `loadForecasting`, pass the visible 90-day demand facts to `renderDemandTrendChart` and clear only that container on read failure.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_forecasting_view_is_prediction_only`

Expected: PASS.

- [ ] **Step 5: Run focused regression and visual checks**

Run: `python -m pytest -q tests/test_admin_console.py tests/test_forecast_three_year_demo.py tests/test_forecasting_workbench_api.py; python -m compileall -q src; git diff --check`

Expected: all selected tests pass, compilation produces no error, and Git reports no whitespace errors.

Open the local forecast sample, verify all three legends and paths appear, inspect the historical/future boundary and confirm that the narrow viewport has no overlapping labels or controls.

- [ ] **Step 6: Commit**

```bash
git add docs/admin-console.html tests/test_admin_console.py docs/superpowers/plans/2026-08-09-forecast-trend-chart.md
git commit -m "feat(admin): chart forecast sales demand"
```

## Self-Review

- Spec coverage: Task 1 replaces both tables, draws the three confirmed series, uses P50 only, supports a point tooltip and preserves mobile reading through a scrollable SVG surface.
- Placeholder scan: no unresolved implementation or test instruction remains.
- Type consistency: all referenced response keys already exist in the forecast workbench API response.
