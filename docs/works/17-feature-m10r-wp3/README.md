# M10-R WP3 订购单草稿、人工确认与交付跟踪 — 交付证据

Commit：`8a72282`（feature/m10r-wp3-purchase-order，基于 main `454b35c`）

## 交付内容

- `src/ecommerce_agent/ordering/models.py`
  - 权威状态机（D-035 单一事实源）：`draft -> awaiting_confirmation ->
    confirmed -> in_transit -> received`；旁路 `cancelled`（draft/
    awaiting_confirmation）、`overdue`（confirmed/in_transit）。
  - `in_transit / received / overdue` 属外部执行事实，必须携带操作者来源引用，
    禁止系统自动推断。
- `src/ecommerce_agent/ordering/gate.py`
  - 草稿生成 Gate：canonical 料号（v35 映射链）+ forecast_run 已完成证据 +
    供货约束（补货策略 supplier_lead_days 或 field evidence actual/manual）。
  - demo 模式显式放行但全链标记“演示参数/未发送”。
- `src/ecommerce_agent/ordering/service.py`
  - 生成/提交确认/人工确认（版本冲突）/取消/状态推进，全部写 `purchase_order_*`
    两张表；确认产生 version+1 并保留原建议数量。
- Schema **v37**（`database.py`）：`purchase_order_drafts` /
  `purchase_order_events`，SCHEMA_VERSION=37，跳过未合入的 v36（M9-R PR #19），
  CONTRIBUTING 权威占号表已登记（下一空闲 38）。
- `src/ecommerce_agent/ordering_api.py`：`/v1/ordering/drafts`（创建/列表/详情/
  submit/confirm/cancel/status），每个操作显式带 store_id，维持租户/店铺隔离。

## 测试与门禁

```powershell
$env:NO_PROXY='127.0.0.1,localhost'; $env:no_proxy='127.0.0.1,localhost';
$env:ALL_PROXY='http://127.0.0.1:9'; $env:HTTP_PROXY='http://127.0.0.1:9';
$env:HTTPS_PROXY='http://127.0.0.1:9'
.\.venv\Scripts\python.exe -m pytest tests\test_purchase_order.py -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

- `pytest tests/test_purchase_order.py`：`15 passed`。
- 迁移 + readonly 回归（test_migrations / product_identity / wp4_readiness /
  readonly_data_ingestion / readonly_data_contract）：`122 passed`。
- `compileall`：退出 0；`git diff --check`：无输出。

## 反证记录

- 临时放宽 Gate（缺料号/补货证据/供货约束仍放行）后，对应阻断测试如期失败；
  还原后通过。
- 让 draft 直接 confirm（跳过 submit）→ `ordering_status_transition_invalid`；
  旧版本确认 → `ordering_version_conflict`；外部状态无 source_ref →
  `ordering_external_state_requires_source`；均还原后通过。
- 状态推进前后 `inventory_plans` / `commerce_orders` 行数不变，确认零外部写。

## 范围

- V1 不发送供应商/ERP、不采购、不付款、不生成生产工单、不修改库存；
  `confirmed` 只表示内部人工确认，不等同供应商已接单。
- 演示参数与未发送标签全链保留；`in_transit / received / overdue` 只能由
  有权限人员带来源引用手工更新。
- 与 M10-R WP1/WP2 的正式接入（补货建议→草稿）待 WP1/WP2 合入后统一接线；
  本分支基于最新 main 可独立合入。
