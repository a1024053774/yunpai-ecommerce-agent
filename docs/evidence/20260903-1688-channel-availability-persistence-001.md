# E-20260903-1688-channel-availability-persistence-001

## 时间与范围

- 日期：2026-09-03（Asia/Taipei）。
- 范围：5043656 hosted OAuth 成功后的真实订单/商品只读探针、A1 渠道可售量首个分页同步、持久化读回和同页幂等重跑。
- 代码/发布：服务器受控 A1 v40 overlay；本轮未修改代码、schema、Nginx 或应用配置。

## Reality gate

- 前置条件：服务器存在 1 条状态为 authorized 且 token 未过期的 1688 连接；callback 新增审计事件。
- 停止边界：只调用平台只读接口和本系统 A1 同步入口；不调用下单、发货、改价、库存写入或付款接口。

## Observed

- Cursor 通过 1688 官方原生日常测试入口生成新入口并在 alitestforisv02:云湃智算 会话中打开；callback 返回 HTTP 200，完成 hosted 授权。
- 服务器新增 alibaba_1688.oauth.authorized 事件；当前连接状态为 authorized=1、error=1，未过期 authorized=1，authorization_mode=hosted。
- 真实只读探针：capabilities、订单列表、商品列表均 HTTP 200。

## Measured

- 首个 A1 分页使用 sync/availability、limit=20：HTTP 200，received=20，mapped=411，rejected=0，applied=20，has_more=true。
- 首次持久化后：channel_availability_snapshots=20，channel_availability_records=411，其中 product scope=20、SKU scope=391。
- 同一 store、同一页无 cursor 重跑：HTTP 200，received=20，mapped=411，rejected=0，applied=0，idempotent=20，issues=0；快照和记录行数均无增长。
- 持久化查询 GET /availability 返回 HTTP 200、411 条记录。
- 语义与完整性检查：非 channel_available 记录=0，负数可售量=0，带 warehouse_code 记录=0，SQLite integrity_check=ok。
- 业务隔离检查：commerce_orders=22,767 未变化，inventory_balances=13 未变化；本轮没有平台写接口调用。

## Gate 与剩余范围

- A1 首个真实分页持久化 Gate：PASS。真实商品/SKU amountOnSale 已进入独立 channel_availability_* 表，查询与同页幂等成立。
- 全量商品回补、增量水位、日对账、异常补偿、生产商家和 WMS/ERP 仓储余额仍为 INCOMPLETE；本证据不宣称全量同步或生产放行。
- 下一步应沿服务返回的 next_cursor 继续受控分页，并在完成一轮后做全量计数、版本水位和对账验收；不应直接把 channel_available 当作 on_hand/reserved/inbound。

## 证据位置

- 服务器数据库：/opt/yunpai-ecommerce-agent/data/agent.sqlite3。
- 服务器发布：/opt/yunpai-ecommerce-agent-releases/1688-a1-v40-82c16d0。
- 未记录授权码、Token、Secret、完整 shop_id/memberId 或原始商品/订单载荷。
