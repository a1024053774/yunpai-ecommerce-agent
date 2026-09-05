# E-20260903-1688-channel-availability-p2-p1-gate-003

## 范围与状态

- 日期：2026-09-03（Asia/Taipei）。
- 目标：复核 A1 渠道可售量的上游总量证据（P2），并判断生产分页同步的可恢复状态（P1）。
- 候选：`codex/unified-agent-multimodal-demo`，基线提交 `cfeceeda38789eafe73676362c8821787aa94ad0`；本轮代码仍为工作树候选，未合并、未部署。
- API 相关核验由 Cursor 执行；本轮没有访问 1688 真实 API、没有部署、没有平台写操作，也没有查看或记录凭据。

## Reality Gate

`STATUS: INCOMPLETE`

### Observed

- `PullBatch` 已增加非敏感的 `upstream_total`；1688 商品列表解析已读取官方响应中的 `totalRecord`、`totalRecords` 或 `total`，同步结果在总量缺失或本地快照数不一致时明确返回对账失败。
- `sync_availability` 仍在一次调用中只读取一个分页；`cursor` 由请求调用方传入，代码没有按 tenant + connector + store + resource 保存可恢复的分页状态或来源水位。
- 现有 `connector_sync_runs` 表只有 `tenant_id`、`connector_id`、`resource`、前后 cursor 和运行结果字段，没有 `store_id`；将店铺编码进 `resource` 或复用与 A1 无关的事实表不符合当前数据边界。
- Cursor 当前会话明确将 P1 标为“需先登记 schema v41”，没有自行修改 `CONTRIBUTING.md` 或占用 v41。

### Measured

- `.venv/bin/python -m pytest tests/test_alibaba_1688.py tests/test_channel_availability.py -q`：`24 passed`，13.08 秒。
- `.venv/bin/python -m pytest tests/test_migrations.py -q`：`18 passed`，8.54 秒。
- `compileall`、`git diff --check` 和 `init_project_management.py --validate`：退出码均为 0。
- P2 的上游总量不一致反例在当前候选中返回 `recon.code=upstream_total_mismatch`；该测试验证了总量信号会被观察到，但不等于已经完成跨页累计对账。
- 服务器既有 A1 证据仍证明一次外部脚本回补得到 `full_backfill_done`，但该证据不改变生产入口缺少持久 checkpoint 的事实。详见 `E-20260903-1688-channel-availability-acceptance-002` 和 `E-20260903-1688-channel-availability-full-001`。

### Inferred

- P1 需要独立的 store-scoped 持久状态边界，至少保存同步模式/窗口身份、当前 cursor、完成水位、上游总量、状态和失败信息；失败时不得推进 cursor，重试必须从同一安全位置继续。
- v40 已经在服务器数据库上执行过。仅修改 `_apply_v40` 不会对已有 `schema_migrations` 中的 40 再执行，因此要把该状态表安全带到已部署数据库，必须使用新的迁移版本，而不是静默扩展 v40。

### Unknown / decision required

- 尚未获得在当前非 `main` 分支修改 `CONTRIBUTING.md`、登记并实施 schema v41 的明确授权。
- 在 v41 授权和实现前，不能声称生产 API 已具备可恢复全量同步，也不能把外部脚本的全量结果当作生产级运行契约。

## Gate 结论

- P2 上游总量信号：**本地候选已实现并有红绿反例；跨页独立对账仍待 P1 状态边界**。
- P1 生产级 checkpoint/watermark：**INCOMPLETE / 等待 v41 授权**。
- WMS/ERP 的 `on_hand/reserved/inbound`：**INCOMPLETE**，继续不写 `inventory_balances`。
- 1688 采购、付款及平台库存写操作：**BLOCKED**。

## 下一步

1. 取得明确授权后，在权威占号表登记 v41，再实现最小 store-scoped checkpoint 迁移、读侧恢复、失败不推进和完成总量对账。
2. 在冻结候选上保留 P1 红态，修复后运行同一反例、相邻回归、迁移/灾备检查，并进行一次独立定向验收。
3. WMS/ERP 继续等待带日期、来源系统/账套/店铺、仓库、SKU/料号、单位及 `on_hand/reserved/inbound` 的真实脱敏来源；采购继续只生成内部 draft，不下单、不付款。

## 候选文件哈希

以下哈希对应本证据记录时的工作树文件：

- `src/ecommerce_agent/alibaba_1688.py`：`e629139c1b300d43e822c7e386e770d85d94fb938f3c162e329188eb63e7b19b`
- `src/ecommerce_agent/alibaba_1688_api.py`：`47617f5c25cb393aacd02229dfa54bee2e0a72d57c03c4292002b3030ff0e814`
- `src/ecommerce_agent/connectors/base.py`：`98b328adb4bdf8f159ca3d8a0e04e13a328ad5929a0e996c09c348219c1e3dbd`
- `src/ecommerce_agent/business/channel_availability.py`：`5b53ed9a31930a3ebff1673e71e0d5ffa29614bb49f27a95df711ce6ac7e756f`
- `tests/test_alibaba_1688.py`：`372ae23a95b9109618e95ced97402f3b52c167292d87dc6d87d9cba72f72841d`
- `tests/test_channel_availability.py`：`150c1150cf8f60329d7311515237d07e34ea745dc2153048d5d59b8fe5daa7d1`
- `src/ecommerce_agent/database.py`：`37e7f8f8b68f065a68e8e8ff1cf7ddd6610e66d53f08e32e2fe5771d361617fd`
