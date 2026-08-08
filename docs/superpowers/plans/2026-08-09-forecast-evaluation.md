# 三年虚拟销售预测评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生成三年虚拟销售数据，保存预测与回测结果，并以中文展示预测方法及实际需求对比。

**Architecture:** 保持既有 forecast run、backtest 与 point 持久化机制。新增受控的本地验收脚本生成确定性订单并触发现有需求重建与预测运行；后台仅消费已有需求、预测和回测 API，不读取库存计划。

**Tech Stack:** Python、FastAPI、SQLite、Pydantic、静态后台页面、pytest。

## Global Constraints

- 虚拟样例不得称为真实平台效果或生产预测结果。
- 留出期为最近 90 天，使用已保存 rolling backtest 实际值与预测值对照。
- 不新增 Schema，不创建采购单，不更新库存，不在后台请求库存计划。
- 前台中文化；API 的机器枚举和存储契约不改。

---

### Task 1: 预测优先的中文后台界面

**Files:**
- Modify: `docs/admin-console.html`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Write the failing test**

断言后台预测区不再包含仓库输入、补货风险、库存快照或 inventory-plan 请求，且存在预测方法与历史预测对比容器。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_admin_console.py::test_admin_console_page_and_audit_api`

Expected: FAIL，因为旧页面仍显示库存快照和补货风险。

- [ ] **Step 3: Write minimal implementation**

移除仓库/补货风险面板和 inventory-plan 调用；将模型方法摘要置于预测结果顶部；渲染冠军模型的持久化滚动回测为“日期、实际需求、预测需求、偏差”表；将可见枚举映射为中文。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_admin_console.py`

Expected: PASS。

### Task 2: 可重复的三年虚拟评估

**Files:**
- Create: `evals/forecasting/run_three_year_demo.py`
- Create: `tests/test_forecast_three_year_demo.py`

- [ ] **Step 1: Write the failing test**

断言固定三年订单生成器产生 1,095 个连续业务日，运行后保存 forecast run、forecast points 和三个 30 天回测窗口；回测实际值不进入未来预测点。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_forecast_three_year_demo.py`

Expected: FAIL，因为验收脚本尚不存在。

- [ ] **Step 3: Write minimal implementation**

用周内季节性、年度周期、平滑趋势和确定性促销峰值生成三年日订单；调用既有 demand rebuild 和 forecast run（30 天预测、3 个滚动窗口）；输出持久化运行 ID、最近 90 天回测汇总及中文说明。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_forecast_three_year_demo.py`

Expected: PASS。

### Task 3: 运行验收与提交

- [ ] **Step 1: Run focused verification**

Run: `python -m pytest -q tests/test_admin_console.py tests/test_forecast_three_year_demo.py tests/test_forecast_persistence.py tests/test_forecasting_workbench_api.py`

- [ ] **Step 2: Run the deterministic demo**

Run: `python evals/forecasting/run_three_year_demo.py --data-dir <temporary directory>`

Expected: 输出虚拟标记、1,095 天、保存的运行 ID、90 天回测对比与模型指标。

- [ ] **Step 3: Verify source hygiene**

Run: `python -m compileall -q src evals; git diff --check`

- [ ] **Step 4: Commit**

Run: `git add docs/admin-console.html evals/forecasting/run_three_year_demo.py tests/test_admin_console.py tests/test_forecast_three_year_demo.py docs/superpowers && git commit -m "feat(forecasting): add three-year evaluation view"`
