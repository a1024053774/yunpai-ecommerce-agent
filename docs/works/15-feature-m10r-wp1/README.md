# M10-R WP1 预测/补货信号适配与数据准备度 — 交付证据

Commit：`31e5da4`（feature/m10r-wp1-signal-readiness）

## 交付内容

- `src/ecommerce_agent/forecasting/readiness.py`
  - 五类准备度投影：预测目标/候选信号/供给约束/交付约束/执行主数据。
  - 证据解析：M7-R `ReadonlyDataService.list_field_evidence` 的
    `readiness:<input_key>` 记录为权威源；无记录时回退按行存在推断，
    并在 `missing_reason` 注明“未登记 field evidence，按行存在推断”。
  - 竞品信号 approved-only（D-025）：仅统计 `competitive_entity_matches`
    `status='approved'` 的去重 subject_sku。
  - 流量经 `listing_revisions.sku_id` 关联到 SKU 覆盖；退款经订单关联。
  - 供应商周期来自补货策略时标为 `manual`；运输周期与料号映射标 `missing`。
- `scripts/m10r_wp1_readiness_report.py`
  - 输出 JSON 与 Markdown 两份准备度报告，候选信号统一标注“未使用（WP2 接线）”。
- `tests/test_readiness.py`
  - 10 个用例覆盖空店、需求、流量、退款、竞品 approved-only、field evidence 覆盖，
    以及“店铺总量不复制成 SKU / 日·月粒度不混”的显式反证。

## 测试与门禁

```powershell
$env:NO_PROXY='127.0.0.1,localhost'; $env:no_proxy='127.0.0.1,localhost';
$env:ALL_PROXY='http://127.0.0.1:9'; $env:HTTP_PROXY='http://127.0.0.1:9';
$env:HTTPS_PROXY='http://127.0.0.1:9'
.\.venv\Scripts\python.exe -m pytest tests\test_readiness.py -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

- `pytest tests/test_readiness.py`：`10 passed`。
- `compileall`：退出 0；`git diff --check`：无输出。

## 报告复跑

```powershell
.\.venv\Scripts\python.exe scripts\m10r_wp1_readiness_report.py `
  --tenant-id tenant-demo --store-id store-demo `
  --db data\wp1-readiness-demo.sqlite3 `
  --json-out docs\works\15-feature-m10r-wp1\readiness-report.json `
  --md-out docs\works\15-feature-m10r-wp1\readiness-report.md
```

产物：`readiness-report.json`、`readiness-report.md`（本目录，空店示例投影）。

## 反证记录

- 临时移除 approved-only 过滤后，
  `test_competitor_signal_requires_approved_match` 如期失败（未批准观测被计入信号）；还原后通过。
- 临时跳过 field evidence 读取后，
  `test_missing_field_evidence_overrides_row_presence` 如期失败（按行存在错误标为 actual）；还原后通过。
- 若把 SKU 覆盖改为按行计数或让 campaign 级信号伪造 SKU，
  `test_sku_coverage_counts_distinct_skus_not_rows` /
  `test_campaign_level_signal_has_no_sku_coverage` 会失败；还原后通过。

## 范围

- 本轮不含公开 API、schema 迁移、第三方依赖，也不做候选信号进 champion 的
  rolling backtest 门禁（归 WP2）。
