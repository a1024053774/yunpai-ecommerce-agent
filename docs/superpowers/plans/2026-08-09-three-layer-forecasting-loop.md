# 销量、库存与补货三层闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已支付订单的日销量预测扩展为按日库存投影和待确认补货草稿，且每层口径可追溯、无自动采购副作用。

**Architecture:** `DemandFactService` 生成只含已关闭自然日的销量事实，`ForecastingService` 只以这些事实训练和输出未来销量。新的入库计划服务保存带预计到货日的供给，`InventoryPlanningService` 以库存快照、未来销量、退货调整和到货计划逐日投影，再持久化待确认补货草稿。管理页在同一库存工作流中依次展示销量、库存和补货三个层次。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、SQLite、原生 HTML/CSS/JavaScript、pytest。

## Global Constraints

- 日销量是 `paid`/`partially_refunded` 且非 `canceled` 的订单行，按 `placed_at` 的 `Asia/Shanghai` 自然日归集；界面明确称为“已支付订单的下单日销量”。
- 仅在已经结束的自然日训练；当天只作为“今日截至当前销量”返回，绝不进入训练样本。
- 退货只通过显式 `return_rate` 调整，默认 `0`；不得从退款金额推断 SKU 件数。
- 在途供给必须有 `expected_arrival_date`；没有到货日的汇总 `inbound` 不得提前进入日库存投影。
- 需求粒度为店铺 + SKU；仓库仅是供给位置，不能把需求复制到多仓。
- 补货结果恒为 `draft`；不得创建真实采购单、付款、库存变更或外部连接器调用。
- 无新增第三方依赖。所有数值用 `Decimal`；所有越界读取按现有租户/店铺/SKU/仓库边界拒绝。
- Schema 31 仅可在负责人登记批准后实现；未获批准时停止在迁移之前，不猜测版本号。

---

### Task 1: 冻结 Schema 31 占号与迁移范围

**Files:**
- Read: `CONTRIBUTING.md: Schema 版本号占用登记`
- Modify after approval: `src/ecommerce_agent/database.py: schema migration dispatch and validation`
- Test: `tests/test_inventory_projection.py`

**Consumes:** 当前 `Database.SCHEMA_VERSION == 30` 及 Schema 29/30 的预测、库存计划表。

**Produces:** 已登记的 Schema 31 迁移边界，且只包含补货闭环所需的新表和列。

- [ ] **Step 1: 在群内请求并取得 Schema 31 的明确登记，不创建迁移。**

  请求内容必须说明新增 `inventory_inbounds` 表、`inventory_planning_policies.return_rate`、`inventory_planning_policies.safety_stock_days`，并说明不会改写 Schema 29/30 的既有表语义。

- [ ] **Step 2: 在未取得登记时验证没有迁移改动。**

  Run: `git diff -- src/ecommerce_agent/database.py`

  Expected: 空输出；不得以“当前版本是 30”为理由自行写 `_apply_v31`。

- [ ] **Step 3: 获批后写失败的迁移测试。**

  ```python
  def test_schema_30_to_31_adds_dated_inbounds_and_policy_fields(tmp_path) -> None:
      db = Database(tmp_path / "forecast.db")
      seed_schema_30_database(db)
      db.initialize()
      assert 31 in db.applied_migrations()
      assert table_has_columns(db, "inventory_planning_policies", {"return_rate", "safety_stock_days"})
      assert table_has_columns(db, "inventory_inbounds", {"expected_arrival_date", "quantity", "status"})
  ```

- [ ] **Step 4: 运行迁移测试，确认因 Schema 31 尚未实现而失败。**

  Run: `python -m pytest -q tests/test_inventory_projection.py::test_schema_30_to_31_adds_dated_inbounds_and_policy_fields`

  Expected: FAIL，原因是迁移或表/列不存在。

- [ ] **Step 5: 用获批版本实现 `_apply_v31`。**

  `inventory_inbounds` 的唯一范围为 `(tenant_id, connector_id, store_id, warehouse_id, sku_id, external_inbound_id)`；字段包含数量、预计到货日、状态、来源时间、载荷哈希、版本和审计时间。为政策表增加非空默认 `return_rate='0'` 与 `safety_stock_days=0`，使用 `_ensure_column` 添加列。迁移验证只断言自身版本和自身表/列，不断言全局最大版本。

- [ ] **Step 6: 运行迁移测试和重复初始化验证。**

  Run: `python -m pytest -q tests/test_inventory_projection.py -k "schema_30_to_31 or initialize"`

  Expected: PASS；历史政策默认退货率为 `0`、安全库存天数为 `0`，重复 `initialize()` 不重复迁移。

- [ ] **Step 7: 提交迁移基础。**

  ```bash
  git add src/ecommerce_agent/database.py tests/test_inventory_projection.py
  git commit -m "feat(forecasting): add dated inbound storage"
  ```

### Task 2: 关闭日需求事实与当前日展示

**Files:**
- Modify: `src/ecommerce_agent/business/demand_facts.py: DemandPolicy and DemandFactService.rebuild`
- Modify: `src/ecommerce_agent/business/forecasting.py: forecast history loading`
- Modify: `src/ecommerce_agent/forecasting_api.py: rebuild and demand response metadata`
- Test: `tests/test_demand_facts.py`
- Test: `tests/test_forecasting_workbench_api.py`

**Consumes:** `placed_at`、订单支付/取消状态、`Asia/Shanghai` 时区。

**Produces:** 只含完整日期的可训练事实及独立的 `today_so_far` 视图。

- [ ] **Step 1: 写失败测试，锁定北京时间当天不训练。**

  ```python
  def test_rebuild_excludes_open_business_day_but_reports_today_so_far(tmp_path) -> None:
      service = make_service(tmp_path, now="2026-08-09T04:00:00+00:00")
      seed_paid_order(service, placed_at="2026-08-08T18:00:00+00:00", quantity="3")
      result = service.operations.demand_facts.rebuild(
          TENANT, store_id=STORE, sku_id=SKU,
          start_date=date(2026, 8, 8), end_date=date(2026, 8, 9),
      )
      assert [item["business_date"] for item in result["facts"]] == ["2026-08-08"]
      assert result["today_so_far"]["business_date"] == "2026-08-09"
      assert result["today_so_far"]["eligible_units"] == "3.00"
  ```

- [ ] **Step 2: 运行测试，确认当前实现会把当天写进训练事实。**

  Run: `python -m pytest -q tests/test_demand_facts.py::test_rebuild_excludes_open_business_day_but_reports_today_so_far`

  Expected: FAIL，原因是当天事实被返回或 `today_so_far` 不存在。

- [ ] **Step 3: 实现可注入时钟与关闭日截断。**

  在 `DemandFactService` 构造函数接收 `now_provider: Callable[[], datetime]`，默认 UTC 当前时刻；转换到 `Asia/Shanghai` 后得出 `closed_through = local_today - 1 day`。`rebuild()` 写入范围截断至 `closed_through`，并用同一订单过滤器计算独立 `today_so_far`。新增 `list_response(tenant_id, *, store_id, sku_id, start_date=None, end_date=None)`，统一返回 `facts`、`today_so_far`、`training_closed_through` 与 `basis`。`forecasting_api.list_demand()` 调用该方法。若请求仅覆盖当天，返回空 `facts` 和完整的 `today_so_far`，不抛出 `demand_no_source_data`。

- [ ] **Step 4: 写并运行下单日口径测试。**

  ```python
  def test_demand_policy_labels_paid_orders_by_placed_day() -> None:
      response = service.operations.demand_facts.list_response(
          TENANT, store_id=STORE, sku_id=SKU,
      )
      assert response["basis"]["event_time"] == "placed_at"
      assert response["basis"]["label"] == "已支付订单的下单日销量"
  ```

  Run: `python -m pytest -q tests/test_demand_facts.py tests/test_forecasting_workbench_api.py -k "closed or placed_day or today_so_far"`

  Expected: PASS。

- [ ] **Step 5: 让预测训练只消费关闭日事实。**

  `ForecastingService.run()` 继续以 `DemandFactService.list_facts()` 为唯一正式训练源；旧的 `preview()` 也需复用相同关闭日边界，不能直接把 `commerce_orders` 的当天行加入历史。响应加入 `training_closed_through` 与需求口径标签。

- [ ] **Step 6: 反证关闭日门禁。**

  临时使 `closed_through` 等于本地当天，运行 Step 2 测试并记录其按预期失败；立即还原后重新运行定向测试。

- [ ] **Step 7: 提交需求口径变更。**

  ```bash
  git add src/ecommerce_agent/business/demand_facts.py src/ecommerce_agent/business/forecasting.py src/ecommerce_agent/forecasting_api.py tests/test_demand_facts.py tests/test_forecasting_workbench_api.py
  git commit -m "feat(forecasting): train only on closed demand days"
  ```

### Task 3: 到货日供给与补货政策服务

**Files:**
- Create: `src/ecommerce_agent/business/inventory_inbounds.py`
- Modify: `src/ecommerce_agent/business/inventory_planning.py: InventoryPlanningPolicy`
- Modify: `src/ecommerce_agent/business/__init__.py`
- Modify: `src/ecommerce_agent/service.py: Operations service assembly`
- Modify: `src/ecommerce_agent/forecasting_api.py`
- Test: `tests/test_inventory_projection.py`

**Consumes:** Schema 31 的 `inventory_inbounds`、当前库存余额和 `InventoryPlanningPolicy`。

**Produces:** 租户隔离、D-014 版本语义的在途供给写入/查询，以及带退货率和安全库存天数的政策。

- [ ] **Step 1: 写失败测试，定义在途供给的时间边界与隔离。**

  ```python
  def test_inbound_is_available_only_on_and_after_expected_arrival_date(tmp_path) -> None:
      service = make_service(tmp_path)
      service.operations.inventory_inbounds.upsert(TENANT, inbound("in-1", quantity="12", arrival="2026-08-12"))
      assert service.operations.inventory_inbounds.for_day(TENANT, STORE, WAREHOUSE, SKU, date(2026, 8, 11)) == []
      assert total(service.operations.inventory_inbounds.for_day(TENANT, STORE, WAREHOUSE, SKU, date(2026, 8, 12))) == Decimal("12")
  ```

- [ ] **Step 2: 运行测试，确认服务尚不存在。**

  Run: `python -m pytest -q tests/test_inventory_projection.py::test_inbound_is_available_only_on_and_after_expected_arrival_date`

  Expected: FAIL，原因是 `inventory_inbounds` 服务不存在。

- [ ] **Step 3: 实现 `InventoryInboundUpsert` 与 `InventoryInboundService`。**

  请求模型包含 `connector_id`、`store_id`、`warehouse_id`、`sku_id`、`external_inbound_id`、`quantity`、`expected_arrival_date`、`status`、`source_updated_at`、`source_id`。服务使用 `decide_write` 或仓库现有等价 D-014 契约：旧版本拒绝、同版本同载荷幂等、同版本不同载荷冲突、新版本应用。只返回同租户、同店铺、同仓库、同 SKU 且状态为 `scheduled`/`in_transit` 的供给。

- [ ] **Step 4: 扩展政策模型并保护历史默认值。**

  `InventoryPlanningPolicy` 新增 `return_rate: Decimal = Decimal("0")`，范围 `0 <= rate <= 1`，以及 `safety_stock_days: int = 0`，范围 `0..365`。`minimum_safety_stock` 继续作为数量下限，两个安全库存规则取较大值，避免破坏既有政策。

- [ ] **Step 5: 运行服务、D-014、跨租户与政策边界测试。**

  Run: `python -m pytest -q tests/test_inventory_projection.py -k "inbound or policy or tenant or version"`

  Expected: PASS。

- [ ] **Step 6: 提交供给与政策服务。**

  ```bash
  git add src/ecommerce_agent/business/inventory_inbounds.py src/ecommerce_agent/business/inventory_planning.py src/ecommerce_agent/business/__init__.py src/ecommerce_agent/service.py src/ecommerce_agent/forecasting_api.py tests/test_inventory_projection.py
  git commit -m "feat(forecasting): manage dated inbound supply"
  ```

### Task 4: 逐日库存投影与待确认补货草稿

**Files:**
- Create: `src/ecommerce_agent/business/inventory_projection.py`
- Modify: `src/ecommerce_agent/business/inventory_planning.py: create_plan and view`
- Modify: `src/ecommerce_agent/forecasting_api.py`
- Test: `tests/test_inventory_projection.py`
- Test: `tests/test_forecasting_workbench_api.py`

**Consumes:** 关闭日训练产生的 `forecast_points`、库存余额、带日期在途、补货政策。

**Produces:** 每日预计库存、缺货日、输入质量说明和持久化的 `draft` 补货计划。

- [ ] **Step 1: 写失败测试，指定每日库存路径。**

  ```python
  def test_projection_adds_inbound_on_arrival_day_and_applies_sales_after_supply() -> None:
      points = p50_points("2026-08-10", ["4", "4", "4"])
      projection = project(available="5", dated_inbounds={"2026-08-11": "10"}, return_rate="0", points=points)
      assert projection.days[0].closing_available == Decimal("1")
      assert projection.days[1].opening_available == Decimal("1")
      assert projection.days[1].scheduled_inbound == Decimal("10")
      assert projection.days[1].closing_available == Decimal("7")
  ```

- [ ] **Step 2: 运行测试，确认投影模块不存在。**

  Run: `python -m pytest -q tests/test_inventory_projection.py::test_projection_adds_inbound_on_arrival_day_and_applies_sales_after_supply`

  Expected: FAIL，原因是 `project` 不存在。

- [ ] **Step 3: 实现纯 `InventoryProjectionService.project()`。**

  输入是 `on_hand`、`reserved`、未来日期的选定分位销量、带日期在途、`return_rate` 和预测日期。输出每一天的期初可用、到货、退货调整、预测销量、期末可用及首次负值日。未知到货日不能进入 `dated_inbounds`，但应写入 `quality.unknown_arrival_inbound_quantity`。

- [ ] **Step 4: 使 `InventoryPlanningService.create_plan()` 使用投影。**

  以政策服务等级选择 P50/P80/P95；目标库存取 `lead_time + review_period` 的选定销量，加上 `max(minimum_safety_stock, mean_demand * safety_stock_days)`，减去投影起点可用量与规划期内已到货供给。先应用 MOQ，再向上取包装倍数。将逐日投影、供给列表、退货率、计算版本写入现有 `inventory_snapshot_json` / `explanation_json`，保持计划 `status='draft'` 和 `external_order_created=false`。

- [ ] **Step 5: 写并运行补货数量、未知到货和无副作用测试。**

  ```python
  def test_plan_ignores_undated_inbound_and_never_creates_external_order(tmp_path) -> None:
      plan = service.operations.inventory_planning.create_plan(
          TENANT, forecast_run_id=run["run_id"], warehouse_id=WAREHOUSE,
      )
      assert plan["explanation"]["quality"]["unknown_arrival_inbound_quantity"] == "8.00"
      assert plan["external_order_created"] is False
      assert no_connector_or_purchase_order_was_created(service)
  ```

  Run: `python -m pytest -q tests/test_inventory_projection.py tests/test_forecasting_workbench_api.py -k "projection or inbound or replenishment or external_order"`

  Expected: PASS。

- [ ] **Step 6: 反证在途时间门禁与 MOQ 顺序。**

  临时将全部在途量加入第一个预测日，确认 Step 5 的未知到货断言失败；还原。再临时把包装倍数放在 MOQ 前计算，确认数值顺序断言失败；还原并复验。

- [ ] **Step 7: 提交库存投影与草稿。**

  ```bash
  git add src/ecommerce_agent/business/inventory_projection.py src/ecommerce_agent/business/inventory_planning.py src/ecommerce_agent/forecasting_api.py tests/test_inventory_projection.py tests/test_forecasting_workbench_api.py
  git commit -m "feat(forecasting): project inventory by day"
  ```

### Task 5: 三层 API 与库存工作台

**Files:**
- Modify: `src/ecommerce_agent/forecasting_api.py`
- Modify: `docs/admin-console.html: forecast controls and rendering`
- Test: `tests/test_forecasting_workbench_api.py`
- Test: `tests/test_admin_console.py`

**Consumes:** 最新预测运行、库存计划、需求事实的 `today_so_far` 元数据。

**Produces:** 同一 SKU 工作流中的销量需求、库存预测和待确认补货建议。

- [ ] **Step 1: 写失败 API 测试，锁定三层独立字段。**

  ```python
  def test_inventory_plan_api_returns_separate_demand_inventory_and_replenishment_layers(client) -> None:
      response = client.get(plan_url, headers=ADMIN_HEADERS)
      assert set(response.json()["layers"]) == {"demand", "inventory", "replenishment"}
      assert response.json()["layers"]["replenishment"]["status"] == "draft"
  ```

- [ ] **Step 2: 运行测试，确认现有响应仍是扁平的。**

  Run: `python -m pytest -q tests/test_forecasting_workbench_api.py::test_inventory_plan_api_returns_separate_demand_inventory_and_replenishment_layers`

  Expected: FAIL，原因是 `layers` 不存在。

- [ ] **Step 3: 实现兼容的三层 API 视图。**

  保留现有顶层字段以避免破坏调用方，同时新增 `layers.demand`、`layers.inventory`、`layers.replenishment`。每层都返回数据水位、口径/政策版本、质量状态和中文 `basis_label`。所有错误继续使用 FastAPI 的 404/409/422 与既有管理员鉴权。

- [ ] **Step 4: 将管理页改为三段连续工作流。**

  需求段显示“已支付订单的下单日销量”、训练截止日、今日截至当前销量和销售需求图；库存段显示期初可用、锁定、带到货日的在途、逐日预计库存、首次缺货日与未知到货提示；补货段显示策略输入、建议数量、预计到货日及“待确认草稿”。不显示 JSON，不提供任何“直接下单”按钮。

- [ ] **Step 5: 写并运行页面结构与中文文案测试。**

  Run: `python -m pytest -q tests/test_admin_console.py tests/test_forecasting_workbench_api.py -k "forecast or inventory or replenishment or placed"`

  Expected: PASS。

- [ ] **Step 6: 浏览器验证。**

  在桌面与移动视口分别检查：三段顺序正确，长图可横向查看，数值不重叠，未知到货与草稿状态清楚可见，且页面没有外部采购操作。

- [ ] **Step 7: 提交接口和工作台。**

  ```bash
  git add src/ecommerce_agent/forecasting_api.py docs/admin-console.html tests/test_forecasting_workbench_api.py tests/test_admin_console.py
  git commit -m "feat(admin): show demand inventory replenishment loop"
  ```

### Task 6: 回归、反证与交付准备

**Files:**
- Modify: `docs/superpowers/specs/2026-08-09-three-layer-forecasting-loop-design.md: append verification evidence`
- Test: `tests/test_demand_facts.py`
- Test: `tests/test_inventory_projection.py`
- Test: `tests/test_forecasting_workbench_api.py`
- Test: `tests/test_admin_console.py`

**Consumes:** Tasks 1-5 的提交与验收证据。

**Produces:** 反证记录、定向与全量测试结果，以及不夸大生产能力的 Draft PR 描述。

- [ ] **Step 1: 运行三层定向测试。**

  Run: `python -m pytest -q tests/test_demand_facts.py tests/test_inventory_projection.py tests/test_forecasting_workbench_api.py tests/test_admin_console.py`

  Expected: 所有定向测试通过。

- [ ] **Step 2: 执行三项反证并还原。**

  依次临时移除取消订单过滤、将当天加入训练、将未知到货量计入首日供给；每次运行对应精确测试并确认失败，恢复后再次通过。记录每项的失败断言与恢复结果到提交正文或 PR 描述。

- [ ] **Step 3: 运行静态与数据库检查。**

  Run: `python -m compileall -q src`

  Run: `python -m pytest -q tests/test_database.py tests/test_forecasting_framework.py tests/test_forecast_persistence.py`

  Run: `git diff --check`

  Expected: 均成功。

- [ ] **Step 4: 按仓库代理隔离要求运行全量测试。**

  Run: 使用 `CONTRIBUTING.md` 第 2 节的代理屏蔽命令后执行 `python -m pytest -q`。

  Expected: 记录实际通过、失败和 xfail 数量；任何非本分支失败先定位，不把超时或失败记为通过。

- [ ] **Step 5: 更新设计证据并提交验收记录。**

  ```bash
  git add docs/superpowers/specs/2026-08-09-three-layer-forecasting-loop-design.md
  git commit -m "docs(forecasting): record three-layer verification"
  ```

- [ ] **Step 6: 创建 Draft PR，不自行合并。**

  PR 描述必须列出 Schema 版本批准信息、需求日口径、退货与到货日限制、反证结果、实际测试数量，以及“补货订单仅为待确认草稿”的安全边界。
