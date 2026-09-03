# E-20260903-1688-channel-availability-full-001

## 时间与范围

- 日期：2026-09-03（Asia/Taipei）。
- 范围：1688 5043656 hosted OAuth 成功后的真实商品列表分页、A1 渠道可售量全量回补、增量水位探针、数据库独立对账和业务表隔离检查。
- 发布：服务器受控 release 1688-a1-v40-82c16d0；本轮没有修改代码、schema、Nginx 或应用配置。
- 证据目录：/opt/yunpai-ecommerce-agent/data/evidence/20260903-1688-availability-full/。
- 安全边界：未记录授权码、Access Token、Refresh Token、AppSecret、完整 memberId/shop_id 或原始商品/订单载荷；未调用平台下单、付款、发货、退款、改价、改库存或取消接口。

## Gate 结论

- A1 渠道可售量全量回补：**PASS**。
- A1 增量水位与同载荷幂等探针：**PASS**。
- SQLite 数据一致性、订单表和 WMS 库存隔离：**PASS**。
- 真实 WMS/ERP 仓储余额接入：**INCOMPLETE**；当前没有可证明来源、时点和 on_hand/reserved/inbound 语义的生产数据源。
- 1688 买家采购、自动下单、付款及平台库存写操作：**BLOCKED**；当前角色/方案/人工审批和资金安全门未满足。

## 全量回补实测

最终状态文件为 full_done.json / state.json，状态为 phase=full_backfill_done、stop_reason=complete、has_more=false。

| 指标 | 结果 |
| --- | ---: |
| pages.jsonl 行数 | 1,063 |
| 成功 HTTP 200 页 | 1,062 |
| HTTP 502 页 | 1 |
| 收到商品快照 | 21,224 |
| 映射渠道可售量记录 | 378,508 |
| 拒绝 | 0 |
| applied | 21,204 |
| 幂等重放 | 20 |
| 商品级记录 | 21,224 |
| SKU 级记录 | 357,284 |

- 唯一一次 HTTP 502 在同一官方游标上重试后成功；没有连续 5xx，也没有把失败页当成空结果。
- 成功页序为 1–1,062，无缺页、重复页或游标转移断裂。applied 少于 received 的 20 条对应重启第一页的 D-014 幂等重放，不是丢数。
- 来源时间范围为 2023-09-14T02:52:11+00:00 至 2026-09-03T10:06:49+00:00；所有快照和记录的本地 revision 为 1，缺失来源时间和异常 payload hash 均为 0。

## 独立数据库对账

对服务器 live SQLite 直接使用只读 sqlite3 查询复核：

| 检查项 | 结果 |
| --- | ---: |
| channel_availability_snapshots | 21,224 |
| channel_availability_records | 378,508 |
| 所有记录 semantic_role=channel_available | 是 |
| 负可售量 | 0 |
| 非空 warehouse_code | 0 |
| 重复记录自然键 | 0 |
| 孤儿记录 | 0 |
| snapshot record_count 不一致 | 0 |
| 记录与快照来源时间/hash/version/租户店铺商品元数据不一致 | 0 |
| commerce_orders | 22,767 |
| inventory_balances | 13 |
| SQLite quick_check | ok |
| 外键错误 | 0 |

商品级与 SKU 级事实分层保持原始粒度，没有把商品级数量广播到 SKU，也没有写入 WMS 型 inventory_balances。

## 增量水位探针

- 水位基准：全量完成后最大稳定 source_updated_at，向后建立 5 分钟窗口；最大水位为 2026-09-03T10:06:49+00:00。
- 实际窗口：1688 modifyStartTime=20260903180649000+0800，modifyEndTime=20260903181149000+0800。
- 真实请求 1 页，HTTP 200、received=6、mapped=65、rejected=0、applied=0、idempotent=6、has_more=false。
- 增量前后 snapshots、records、commerce_orders 和 inventory_balances 均未变化；orders_unchanged=true、inventory_unchanged=true。

## 服务与业务边界

- yunpai-ecommerce-agent.service 当前 active；/health=200、/ready=200。
- A1 只持久化 1688 的 channel_available 事实。amountOnSale 不等于 WMS 的 on_hand、reserved 或 inbound，不能驱动正式采购数量。
- 《商品库存管理.xlsx》仍只作为字段映射样本，详见 E-20260903-wms-source-workbook-001；在拿到真实 WMS/ERP 来源系统、账套/店铺范围、数据时间、仓库、SKU/料号、单位及明确库存语义前，不导入生产、不回填 inventory_balances。
- 下一真实开发切片是 WMS/ERP 只读来源核验与 fail-closed 导入设计；采购先做内部 draft/人工导出，1688 采购执行必须在取得采购服务商角色、方案、买家 OAuth 和人工批准后从 preview 开始。

## 证据位置

- 本地证据：docs/evidence/20260903-1688-channel-availability-full-001.md。
- 服务器证据：/opt/yunpai-ecommerce-agent/data/evidence/20260903-1688-availability-full/{gate,incremental,recon,full_done,state,pages}.json*。
- 服务器数据库：/opt/yunpai-ecommerce-agent/data/agent.sqlite3。
