# E-20260903-1688-channel-availability-acceptance-002

## 范围

- 日期：2026-09-03（Asia/Taipei）。
- 审查者：独立 acceptance-auditor 01a06835-7539-7953-8ab5-2c4ef96d0296。
- 冻结目标：codex/unified-agent-multimodal-demo @ cfeceeda38789eafe73676362c8821787aa94ad0，以及服务器 A1 overlay 与脱敏 evidence。
- 审查为只读；未修改文件、数据库、服务器或分支。

## 结论

`STATUS: INCOMPLETE`

真实服务器证据足以证明本轮返回游标链已遍历完成、数据已落库且增量幂等，但还不能证明当前生产 API 自身拥有可恢复的全量运行契约，也缺少独立的上游总量或商品 ID manifest oracle。因此不得把“服务器脚本完成一次回补”扩大为“生产级全量同步能力已验收”。

## 已证明

- 服务 active，回环 health/ready 均为 HTTP 200。
- pages.jsonl 共 1,063 次请求，1,062 次 HTTP 200、1 次 HTTP 502；502 后同一游标重试成功，最终 full_backfill_done / complete / has_more=false。
- received=21,224、mapped=378,508、applied=21,204、idempotent=20、rejected=0。
- SQLite snapshots=21,224、records=378,508，其中 product=21,224、SKU=357,284；semantic_role 全部为 channel_available，负数、重复键、孤儿和元数据不一致均为 0，quick_check=ok，外键错误为 0。
- 增量窗口真实返回 6 个商品、65 条记录，applied=0、idempotent=6；前后 snapshots、orders 和 inventory_balances 均未变化。
- 本地聚焦测试 tests/test_alibaba_1688.py 与 tests/test_channel_availability.py 为 23 passed，但这些测试属于 mock 边界证据。

## Findings

### P1：生产入口未拥有可恢复的全量运行状态

- src/ecommerce_agent/alibaba_1688.py 的 sync_availability 当前只执行单页读取。
- 全量续跑、HTTP 502 恢复和增量水位由服务器 evidence 目录中的外部脚本承担。
- 当前候选没有按 tenant + connector + store 隔离的生产 checkpoint/watermark 契约；进程退出后的恢复依赖脚本与 sidecar 文件。
- 最小修正方向：复用现有权威同步运行表时必须证明 store 隔离、失败不推进 cursor、完成水位和恢复读取；若现有表不能承载，必须登记新的 schema 版本，不能复用 v40 或把游标塞进 readonly manifest、A1 事实表或 resource 字符串。

### P2：缺独立上游总量/ID oracle

- 现有 recon 验证返回游标链、本地数据库计数和语义一致性。
- 它不能单独证明上游没有漏页或漏商品。
- 最小修正方向：优先使用平台列表响应的 totalRecords 作为非敏感上游总量，或生成脱敏的商品 ID digest/manifest；总量或 manifest 不一致必须使对账失败。

## 当前 Gate

- A1 单次回补数据与增量幂等证据：PASS。
- A1 生产级全量 checkpoint/watermark 与独立上游 oracle：INCOMPLETE。
- 真实 WMS/ERP on_hand/reserved/inbound：INCOMPLETE。
- 1688 采购、付款和库存写操作：BLOCKED。

## 下一步

1. 先保留当前失败结论，不通过文档措辞把它改成 PASS。
2. 让 Cursor 负责 API 契约侧：传播平台 totalRecords 并建立会失败的总量对账；判断现有 connector_sync_runs 是否能安全承载店铺 checkpoint。
3. 若需要新表或 store_id 列，先取得非 main 分支修改 CONTRIBUTING.md 并登记新 schema 版本的明确授权。
4. 修复后重新跑聚焦反例、相邻回归和真实只读服务器探针，再进行一次针对 P1/P2 的定向独立复审。
