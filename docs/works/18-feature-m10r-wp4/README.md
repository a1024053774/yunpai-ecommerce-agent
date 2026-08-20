# M10-R WP4 费用底账、三层利润政策与经营决策数据层 — 交付证据

Commit：`ec5ad37`（feature/m10r-wp4-profit-ledger，基于统一分支 WP1-WP3）

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
- 剩余：端到端真实/人工/演示/缺失场景与浏览器截图证据（WP4-04），
  以及经营建议卡片的模型解释接入（模型 key 配置后）。
