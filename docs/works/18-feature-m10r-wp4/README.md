# M10-R WP4 费用底账、三层利润政策与经营决策数据层 — 交付证据

Commit：`2618f7e`（feature/m10r-wp4-profit-ledger，基于统一分支 WP1-WP3）

## 交付内容

- `src/ecommerce_agent/profit/models.py`
  - 权威费用类别与唯一利润层映射（D-035 单一事实源）：销售层/经营层/财务最终层，
    每类只归属一层，禁止跨层重复扣除。
  - 正式口径必需费用集：任一必需费用缺失，对应层“暂不可核算”，缺失不补零。
  - 收入确认口径：签收确认收入（`signed_receipt`），收入/退款冲减必须带订单引用。
- `src/ecommerce_agent/profit/service.py`
  - canonical ledger：`profit_ledger_entries` 单一账本，三层利润均由同一账本推导。
  - 正式/demo 物理隔离：demo 来源不能进 formal 范围；试算结果全链标注
    “试算（演示参数）”。
  - 对账：同订单同类别重复入账、退款冲减缺少对应签收收入等双算/漏算问题检测。
  - 幂等：entry_key 唯一，重复入账拒绝。
- Schema **v38**（`database.py`）：`profit_policies` / `profit_ledger_entries`，
  SCHEMA_VERSION=38；CONTRIBUTING 已登记（下一空闲 39）。
- `src/ecommerce_agent/profit_api.py`：`/v1/profit/policies`、
  `/v1/profit/ledger/entries`、`/v1/profit/projection`、`/v1/profit/reconciliation`。

## 测试与门禁

```powershell
$env:NO_PROXY='127.0.0.1,localhost'; $env:no_proxy='127.0.0.1,localhost';
$env:ALL_PROXY='http://127.0.0.1:9'; $env:HTTP_PROXY='http://127.0.0.1:9';
$env:HTTPS_PROXY='http://127.0.0.1:9'
.\.venv\Scripts\python.exe -m pytest tests\test_profit.py -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

- `pytest tests/test_profit.py`：`9 passed`（三层计算、缺失不补零、demo 隔离、
  收入需订单、幂等、双算检测、退款无收入标记、demo/formal 边界、单层归属）。
- 迁移 + readonly + 订购单联动回归：`60 passed`。
- `compileall`：退出 0；`git diff --check`：无输出。

## 反证记录

- 缺失费用置 0 无法让 Gate 通过：必需费用缺失时层状态为 missing、amount 为 null。
- demo 参数不能进入 formal 查询：demo 来源入 formal 范围被拒，投影物理隔离。
- 退款冲减重复入账 → 对账报 `duplicate_order_category_entry`；
  退款无对应签收收入 → 报 `refund_without_signed_revenue`。
- 同一 entry_key 重复入账 → `profit_ledger_entry_duplicate`。
- 三层利润同源：同一 ledger 推导，类别单层归属断言防跨层重复扣除。

## 范围

- 本提交完成数据层：政策、canonical ledger、三层利润投影、对账与 API。
- 经营决策台页面（WP4-03 主体）已接入 admin 控制台左栏：新增「经营决策台」入口，
  只读整合三层利润 / 对账 / 订购单 / 库存风险 / 广告 ROI 可用性，并输出由固化事实
  推导的经营建议卡片（含依据、缺口、需确认人），页面无任何执行按钮、不隐式运行。
- 端到端场景（WP4-04 主体）：`scripts/m10r_wp4_e2e.py` HTTP 全链复跑，
  9/9 通过（正式三层利润 / 缺失不补零 / demo 隔离与标签 / 双算对账 /
  订购单 Gate 与状态机 / 写屏障），证据见同目录 `e2e-scenario-20260821.json`。
- 浏览器截图证据（WP4-04）：`scripts/m10r_wp4_dashboard_screenshot.py`
  （Playwright + Edge headless）产出 `screenshots/m10-decision-formal-20260821.png`
  与 `m10-decision-demo-20260821.png`，DOM 校验 KPI/三层利润/对账/建议卡片均已渲染。
- 剩余：经营建议卡片的模型解释接入（模型 key 配置后）。

## 2026-08-24 门禁 #9/#10 补交付（PR #24 最终门禁）

按闫睿涵最终门禁方案补齐生产消费入口，证据日期 2026-08-24：

### #9 真实外生信号接生产 Gate

- 新增 `src/ecommerce_agent/forecasting/signal_adapter.py`：
  `TrafficSignalAdapter` 把 M7-R 已落库的日级流量事实投影为候选信号序列，
  严格按 tenant/store/SKU/date 隔离（经 `listing_revisions` 绑定 SKU）；
  信号值=当日曝光/此前曝光均值（只依赖过去，构造上无未来泄漏）；
  只使用 `data_as_of >= metric_end` 的行；无字段证据按行存在推断 actual，
  字段证据为 manual/demo 时对应降级。
- `ForecastRunService.run()` 生产路径不再硬编码空信号：无真实信号时返回
  missing/not_used（baseline 照跑），有信号时送 `SignalGate` 无泄漏准入，
  结果写入 run 的 `signal_candidates` / `signal_champion_reason`。
- 反例测试 `tests/test_signal_adapter.py`（8 个）：空数据→None、跨店/SKU 错配
  隔离、陈旧 as-of 丢弃、demo 证据降级、未来泄漏拒绝、劣于 baseline 拒绝等。

### #10 结构化经营建议接模型

- 新增 `src/ecommerce_agent/decision_advisor.py`：复用 `ModelGateway.generate_json`，
  输入为固化事实（利润投影/对账/订购单/库存风险/营销可用性），输出
  “建议/依据/数据缺口/确认人/下一步”结构化卡片；模型只解释、绝不修改任何数值。
- 新增 `POST /v1/decision/suggestions`（`decision_api.py`，审计 `decision.suggestions.requested`）。
- MODEL_ENABLED=false、无模型、超时或输出不合法时显式返回
  `available=false` + 机器可读 reason（“模型建议不可用”），并移除浏览器
  `if/else` 语义建议（D-034/门禁 #10-3）。
- 测试 `tests/test_decision_advisor.py`（6 个）：禁用/无模型/错误/非法输出反例、
  固定模型替身成功路径、事实不可变、API 降级与审计。
- 真实模型 smoke（DeepSeek 测试 key，仅环境变量注入、未提交未落盘）：
  `POST /v1/decision/suggestions` 返回 3 条中文结构化建议，依据/缺口/确认人
  与页面事实一致。

### 浏览器证据（重出）

- 截图 `screenshots/m10-decision-formal-20260824.png` /
  `m10-decision-demo-20260824.png`（Playwright + Edge headless）：
  正式口径三层利润 500.00 / 330.00 / 310.00 可用、对账双算检测、订购单
  “未发送（演示参数）”标签、3 条真实模型建议；demo 口径全链“试算（演示参数）”。
- 管理台 final 净利润“受限”展示修复：无 `finance:final_profit:read` 时显示
  “受限”而非“缺失”；本机截图通过 `FINAL_PROFIT_READ_ADMIN_IDS=local-admin`
  显式开发配置授予后展示完整三层利润（#11 边界不变，默认仍拒绝）。

### 验证

- 定向回归（9 个测试文件，含本次新增）：`84 passed`；`compileall` 退出 0；
  `git diff --check` 无输出。

### 未完成（记录在案）

- 全量回归未重跑（需 30–60 分钟）；WP5 独立验收未执行；两者完成前不构成
  M10-R 签署或生产放行。

## 2026-08-24 下午：决策台销量/预测面板、单品下钻与粒度守卫（补 WP4 验收）

对照任务书 WP4 验收标准补齐，证据随 2026-08-24 截图更新：

### 销量趋势与预测面板

- 经营决策台新增「销量趋势与预测」面板（任务书「展示店铺与单品销量趋势、预测、
  库存健康、广告 ROI、三层利润和订购单」）：按 SKU 展示近 7/30 日需求事实合计与
  最新预测 7/14/30 日 P50，预测状态（completed/degraded/未运行）；数据全部来自
  需求事实与固化 forecast run，只读、不隐式重跑。

### 点击单品下钻

- 点击趋势面板任意 SKU 行打开「单品下钻」面板（任务书「点击单品可追到订单/需求、
  forecast、inventory snapshot、广告/竞品、费用和单据版本」）：
  需求历史（近 14 天）、最新预测（champion + 7/14/30 P50）、回测（最近 4 origin
  WAPE）、库存计划、相关订购单草稿（含未发送标签）、竞品分析、费用条目。
- 新增只读接口 `GET /v1/profit/ledger/entries`（`profit_api.py` + 
  `ProfitService.list_entries`）：财务最终层金额对无 `finance:final_profit:read`
  权限的管理员脱敏（amount=null、restricted=true）并审计
  `profit.ledger.entries.final_denied`（#11 边界保持一致）。

### 利润粒度混用守卫

- `ProfitService.projection` 新增确定性守卫：同一投影内存在 ≥2 种已声明粒度
  （store/order/day/month 等）时抛 `mixed_granularity_projection`，拒绝静默
  混算（任务书「单品、订单、日/月和店铺级金额只能按明确分摊政策汇总，不能静默
  混粒度」）；等批准的分摊政策落地后再放行。

### 验证

- 新增测试：`test_mixed_granularity_projection_rejected`、
  `test_ledger_list_entries_filters_by_sku_and_period`，
  `test_ledger_entries_api_masks_final_without_capability`、
  `test_ledger_entries_api_shows_final_with_capability`。
- M10 定向回归 10 个文件：**88 passed**；compileall / git diff --check 通过。
- 本地演示数据（不入库）：为 e2e-store/E2E-SKU 补 60 天需求事实并通过正式 API
  `POST /v1/forecasting/runs` 生成 forecast（champion=croston，7/14/30 P50=
  87/174/372，信号 admission=insufficient_evidence/not_used —— 生产无信号路径
  正常）；截图覆盖正式口径、单品下钻、演示口径。

### 浏览器证据

- `screenshots/m10-decision-formal-20260824.png`：三层利润 + 销量趋势与预测面板。
- `screenshots/m10-decision-drilldown-20260824.png`：点击 E2E-SKU 后的单品下钻
  （需求历史/预测/回测/订购单/竞品/费用）。
- `screenshots/m10-decision-demo-20260824.png`：演示口径全链标签。

### 反例 / 变异测试（mutation 红→绿，WP5 验收要求）

破坏关键边界时应失败、还原后恢复通过。2026-08-24 复核 5 项，均满足：

| 边界 | 破坏方式 | 破坏后测试 | 还原后 |
|---|---|---|---|
| 信号门禁未来泄漏 | 把“未来日期拦截”改成恒不拦截 | `test_future_signal_is_rejected_as_leakage` 失败（返回 insufficient_evidence） | 通过 |
| 信号适配器 SKU 隔离 | 把 `r.sku_id=?` 改成恒真 | `test_cross_store_and_sku_isolated` 失败（跨 SKU 串数据） | 通过 |
| 利润粒度混用守卫 | 把守卫改成恒不触发 | `test_mixed_granularity_projection_rejected` 失败（不再抛错） | 通过 |
| final 净利润服务端脱敏 | 把权限判断改成恒假 | `test_final_profit_capability_default_denies` 失败（restricted 变 False） | 通过 |
| 模型建议“未启用即不可用” | 把禁用判断改成恒假 | `test_model_disabled_returns_unavailable_without_calling_model` 失败（available 变 True） | 通过 |

全部还原后，相关 5 个测试文件 42 passed。
