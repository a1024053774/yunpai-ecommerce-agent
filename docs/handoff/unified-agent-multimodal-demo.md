# 统筹 Agent 多模态 Demo：公网入口、部署与使用说明

适用日期：2026-08-29。

本说明对应受鉴权的 virtual 产品测试实例，不代表真实淘宝/天猫渠道、真实经营数据、平台写权限、长稳或正式生产 Gate 已放行。访问令牌、Cookie、管理员密钥和模型 API Key 不写入 GitHub、飞书正文、截图或工单。

## 1. 当前交付版本

- GitHub 分支：demo/main-multimodal-product-demo
- 公网已部署代码：c3812313502493571f968c025969974893effd47
- 服务器部署回执：/opt/yunpai-ecommerce-agent/.deploy-revision
- 不可变发布目录：/opt/yunpai-ecommerce-agent-releases/c3812313502493571f968c025969974893effd47
- 当前应用、数据库 schema、模型和能力状态以 /health、/ready、能力 API 与 .deploy-revision 为权威来源；2026-08-29 验收时观测到包版本 0.30.0、schema 39。

- 2026-08-29 后续修复：统筹 Agent 历史会话横向条已限制在聊天工作区宽度内，窄屏下可横向滚动到最右侧卡片；修复提交为 `c3812313502493571f968c025969974893effd47`。

## 本次 session：功能与实测总结

本次工作以最初的统筹 Agent 为基础，没有另起一套 mock 客户端，而是把最新 Demo 后端能力接回同一套 FastAPI 服务、SQLite 数据、租户隔离、工具注册和审计链路，并整理为客户可直接使用的公网入口。

- **客户入口统一**：`/admin` 是统筹 Agent 主入口，`/admin/advanced` 是同一服务的高级管理后台；`/app` 统一跳转到 `/admin`，避免误进入 AgentLoop；`/customer-test` 保留为图片客服与文案润色测试入口。
- **后端能力接入**：统筹 Agent 可按需调用商品/SKU、多仓库存、订单/物流/售后、竞品、营销投放、利润对账、运营辅助、经营指标、流量实验、需求预测、库存计划、商品生命周期建议和建议审计等只读能力。高级管理页面展示同一批结构化结果、来源、状态、新鲜度和审计信息。
- **多模态交互**：`/admin` 输入框支持直接粘贴 PNG/JPEG/WebP，不要求先上传；Qwen2.5-VL 只生成脱敏、非权威的图片观察，后续轮次可继续追问，但图片观察不会自动成为商品、库存、订单或支付事实，也不会单独授权写操作。
- **客服润色**：`/customer-test` 已接入 Qwen2.5-VL 图片识别和 Qwen3-14B 文案润色，并展示调用、采用、模型和耗时状态；润色只改变表达，不改变已核实的业务数字。
- **数据装载与实测**：装载 virtual 演示数据后，逐项验证了商品、库存、订单、营销、利润、预测、库存计划、生命周期、审计和流量实验调用；流量实验直接读取已固化的效果值、方向和置信区间，不在前端重算统计。
- **体验修复**：历史会话条由内容撑宽的问题已修复，聊天区域现在限制在工作区宽度内并支持横向滚动；公网实测 `clientWidth=612`、`scrollWidth=3960`、`overflow-x=auto`，可滚动到最右侧会话卡片。
- **验证与发布**：相关整合验收、workspace/presenter/multimodal 回归、`compileall`、`git diff --check` 和公网 `/health`、`/ready` 检查均已完成。部署链路为 GitHub 分支 → 不可变 release → 备份校验 → systemd → `127.0.0.1:8767` → Nginx TLS `:8800` → Cookie 门禁 → 客户浏览器；访问 Cookie 有效期为 7 天，访问链接和凭据不写入仓库或文档。

本次最终收口包括：

- /admin 接入商品、订单、库存、竞品、营销、利润、运营辅助、指标、流量实验、需求预测、库存计划、生命周期建议和建议审计。
- /admin 可直接在输入框按 Ctrl/⌘+V 粘贴 PNG/JPEG/WebP，不要求先点“上传图片”。
- 图片经 Qwen2.5-VL 形成非权威观察；脱敏后的观察文本可在后续轮次继续使用，但不会变成业务事实或执行授权。
- /customer-test 实际接入 Qwen2.5-VL 和 Qwen3-14B 润色，并显示是否调用、是否采用、模型和耗时。
- 流量实验读取已固化的 effect_estimate / confidence_interval，方向、变化值和区间按实验 ID 绑定；不重算统计。

## 2. 正确公网入口

- https://129.211.3.209:8800/ → 302 /admin
- https://129.211.3.209:8800/app → 302 /admin
- https://129.211.3.209:8800/admin：统筹 Agent 主入口
- https://129.211.3.209:8800/admin/advanced：高级管理后台
- https://129.211.3.209:8800/customer-test：图片识别与客服润色测试

AgentLoop 的独立域名未改；不要再把 IP 实例的 /app 当成 AgentLoop 客户入口。无有效 Cookie 时，/admin、/admin/advanced 和 /customer-test 均返回 403。

## 3. 安全取得访问链接 / Cookie

访问链接只保存在服务器。仅在受控终端执行：

    ssh yunpai
    sudo stat -c '%a %U %G %n' /etc/yunpai-product-demo/access-url
    sudo cat /etc/yunpai-product-demo/access-url

要求：

- 文件保持 0600 root:root。
- 链接只通过企业密码管理器、受控私聊或现场交接发送。
- 第一次打开短期链接后，Nginx 写入 7 天 Secure / HttpOnly / SameSite=Lax Cookie，并跳转 /admin。
- 后续只使用干净的三个产品入口，不复制或查看 Cookie 内容。
- 退出使用 /product-demo/logout；令牌轮换后重新打开维护者提供的新链接。

## 4. 使用说明

### 4.1 统筹 Agent

1. 进入 /admin。
2. 可展开“补充业务范围”，填写店铺 ID、SKU、订单 ID；新会话若不需要旧范围，应清空这些可选字段。
3. 直接描述经营问题。只读查询会按需调用一项或多项后台能力；涉及退款、改价、采购、付款、发布等写操作时只给出确认提示。
4. 图片咨询直接把图片粘贴进消息输入框；出现“已准备好，直接发送即可”后输入问题并发送。
5. 下一轮可说“继续基于上一张图说明”。系统只恢复已脱敏的非权威观察，不保存 base64 到模型历史。
6. 需要看完整结构化证据时点击“高级管理”。

演示数据范围：

- qingchuan-flagship-001：商品、订单、库存、营销、利润、流量实验等。
- QC-AF5-WHITE / QC-ORDER-1001：商品、多仓库存与物流示例。
- YP-SKU-TRAFFIC-001：固化流量实验示例。
- virtual-shop-001 / YP-SKU-001：需求预测、库存计划和生命周期建议示例。
- sim-rec-001：生命周期建议与审计示例。

### 4.2 高级管理

/admin/advanced 与统筹 Agent 使用同一 FastAPI 服务、SQLite 数据库、租户和工具实现，不是另一套 mock 后台。重点页面：

- 功能模块：查看 13 个可用业务模块的职责、边界和 Agent 工具。
- 需求预测：查看固化 run、区间、回测、库存建议与质量原因。
- 商品经营：查看 M9-R 读模型、门禁、诊断、生命周期建议和审计；缺证据时 fail closed。
- 营销投放 / 利润对账：查看活动日指标、ROAS、CTR、费用、管理利润和差异任务。
- 流量实验：读取实验、样本、effect、可信区间、质量门禁、反证和新鲜度；页面查询不会运行新分析。

### 4.3 客服图片与润色

进入 /customer-test，选择虚拟演示对象，可上传或直接粘贴图片。回复下方重点查看：

- Qwen 视觉：已解析
- Qwen 润色：已采用，或已调用、原文未变
- 风险、上下文、证据和决策详情

客服测试不会向真实平台发送消息。润色只调整表达，不得改变已核实的价格、库存、物流、退款或经营数字。

## 5. 回复状态语义

- pending：处理中，不是最终结果。
- control_response：本轮没有查询实时业务事实，例如图片描述、澄清或动作确认；不能当成后台业务事实核验。
- verified_final：所需只读任务均为 success 或已核实 no_data，最终正文通过事实/状态校验。模型草稿不一致时会使用确定性事实摘要并显示“安全降级”，仍可为 verified_final。
- incomplete：任务失败、只完成部分或无法形成有效正文；不得当成已完成。

刷新历史后，processing 仍保留 delivery_mode、completion_status、工具事件、Vision 状态和降级原因。

## 6. 当前部署链路

    GitHub demo/main-multimodal-product-demo
      → 不可变 release 目录
      → 停止 systemd 服务
      → 源码快照 + 0600 环境备份
      → 旧代码创建并验证停机加密 .ypbak
      → rsync 到 /opt/yunpai-ecommerce-agent
           保留 .env / .venv / data / backups
      → compileall / 新代码创建并验证停机加密 .ypbak
      → 写入 .deploy-revision
      → systemd: yunpai-ecommerce-agent.service
      → FastAPI 仅监听 127.0.0.1:8767
      → Nginx TLS :8800 + 访问链接/Cookie 门禁
      → 客户浏览器

推理链路：

- 127.0.0.1:18085：Qwen3.6-35B，统筹规划与最终生成。
- 127.0.0.1:58080：Qwen3-14B，受事实锚保护的客服润色。
- 127.0.0.1:58081：Qwen2.5-VL，非权威图片观察。

上述端口通过受控隧道使用，不直接暴露公网。Neo4j 是可替换的图谱能力；本 Demo 的生产回答链路使用运行时 SQLite RAG，因此 Neo4j 连接失败不阻断当前 virtual Demo。

## 7. 发布步骤与回滚

1. 确认 GitHub 目标提交和工作树范围。
2. 用 git archive 写入新的不可变 release 目录。
3. 停止 yunpai-ecommerce-agent.service。
4. 备份旧源码和 0600 .env；用旧代码执行 backup --require-stopped 与 backup-verify。
5. rsync --delete release 到应用目录，显式排除 .env、.venv、data、backups 和部署回执。
6. 运行 compileall；用新代码再次创建、验证停机加密备份。
7. 写入 .release-commit 和 .deploy-revision，启动 systemd。
8. 检查回环 /health、/ready，再检查公网重定向、403 门禁和有效 Cookie 下三个页面。
9. 执行商品/库存/订单、营销/利润、预测/计划、生命周期/审计、流量、统筹粘贴图片、跨轮图片和客服 Vision/Polish smoke。

失败时停止新服务，使用 .deploy-revision 中的源码归档和匹配 schema 的已验证 .ypbak 回滚；不要临时关闭公网认证。

## 8. 2026-08-29 验收结果

- 最终代码全量：1511 passed, 1 skipped；唯一 skip 为既有条件跳过。
- 最终变更相关回归：75 项 workspace/presenter/multimodal 通过；compileall、git diff --check、project-to-act 校验和 design-integrity scanner 通过。
- 公网：/ 与 /app 均 302 /admin；无 Cookie 的三个页面均 403；服务 /health、/ready 均 200。
- 统筹 Agent：商品/多仓库存/订单、营销/利润、预测/库存计划、生命周期/审计和流量实验均通过真实公网只读调用。
- 流量实验：两份固化分析均显示实验 ID、状态、质量门禁、结论、新鲜度、方向、变化值和区间；数据库确认 delivery_mode=verified_final、completion_status=completed、工具 success。
- 图片：Ctrl/⌘+V 直接粘贴成功，Vision applied；不附图的后续轮次继续使用上一张图且未调用经营工具。
- 客服：Qwen2.5-VL applied，Qwen3-14B polish applied。
- 浏览器：/admin、/admin/advanced、/customer-test 的 console error/warning 均为 0。
- 飞书文档：`统筹 Agent 多模态 Demo：公网入口、部署与使用说明` 已保存到云端；正文本轮追加 17 张图片（15 张选定验收图、1 张滚动修复图、1 张原始问题复现图），评论数为 0。既有历史图片块未删除。
- 历史会话滚动修复：红态公网 `/admin` 的聊天容器被内容撑到约 4000px，滚动容器 `clientWidth == scrollWidth`；修复后会话条 `clientWidth=612`、`scrollWidth=3960`、`overflow-x=auto`，实际滚动到 `scrollLeft=3348.5`，最右侧卡片进入可视区。
- 修复回归：`tests/test_workspace_conversations.py` 先红后绿；相关 workspace/presenter/multimodal 回归 `79 passed in 33.23s`，compileall 与 `git diff --check` 通过。该项是对 E-20260829-001 整合验收的追加修复，不重新声称已重跑全量测试。

关键截图：

- docs/evidence/20260829-845a0f5-admin-direct-paste-vision-badge.png
- docs/evidence/20260829-845a0f5-admin-paste-cross-round-live.png
- docs/evidence/20260829-845a0f5-admin-product-inventory-order-live.png
- docs/evidence/20260829-845a0f5-admin-marketing-profit-live.png
- docs/evidence/20260829-845a0f5-admin-forecast-inventory-plan-live.png
- docs/evidence/20260829-845a0f5-admin-lifecycle-audit-live.png
- docs/evidence/20260829-845a0f5-admin-traffic-verified.png
- docs/evidence/20260829-845a0f5-advanced-overview.png
- docs/evidence/20260829-845a0f5-advanced-modules.png
- docs/evidence/20260829-845a0f5-advanced-forecasting.png
- docs/evidence/20260829-845a0f5-advanced-product-lifecycle-loaded.png
- docs/evidence/20260829-845a0f5-advanced-marketing.png
- docs/evidence/20260829-845a0f5-advanced-finance.png
- docs/evidence/20260829-845a0f5-advanced-traffic-lab.png
- docs/evidence/20260829-845a0f5-customer-vision-polish.png
- docs/evidence/20260829-c381231-admin-history-scroll-fixed.png

对应验收台账：E-20260829-001、E-20260829-002 / G-UNIFIED-AGENT-DEMO-001、G-UNIFIED-AGENT-DEMO-002。
