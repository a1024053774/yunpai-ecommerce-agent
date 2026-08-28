# 统筹 Agent 多模态 Demo 部署与使用说明

适用对象：2026-08-28 合并后的统筹 Agent 与多模态客服 Demo。
管理员密钥、Basic Auth 密码、模型 API Key 和 4090 推理服务地址必须通过受控渠道提供，不进入 GitHub。

## 入口

部署后由同一个服务提供三个入口：

- /admin：统筹 Agent 主入口，用于店铺经营、商品、订单、库存、客服、流量、预测和建议的统一查询。
- /admin/advanced：高级管理后台，用于商品经营工作台、数据准备度、需求预测、客服接管、知识与 SOP、质检和发布门禁。
- /customer-test：受控客服体验，支持 PNG/JPEG/WebP 图片、Qwen 视觉识别和 Qwen 润色；只写入隔离的虚拟测试数据，不向平台发送消息。

当前公网产品测试实例入口：
https://129.211.3.209:8800/

根路径会进入 /admin。当前公网 Demo 由受保护访问入口签发 7 天 Secure/HttpOnly/SameSite Cookie；访问入口本身是凭据，只通过部署维护者的安全渠道提供，故不写入仓库或本文。授权后 /admin、/admin/advanced、/customer-test 可直接使用。上游应用的管理员免登录只限 Nginx 回环链路；本地或其他部署若启用管理员认证，应按实际配置提供管理员凭据。

## 第一次使用

1. 打开部署维护者提供的受保护访问入口；浏览器获得安全 Cookie 后进入 /admin。当前公网 Demo 不需要在页面输入管理员密钥。
2. 直接描述目标，例如“汇总当前库存风险和最近订单”，统筹 Agent 会按需选择只读业务能力并展示处理摘要。
3. 查询商品经营、预测或生命周期建议时，可直接提出自然语言问题；这些能力只读取已固化证据，不会自动创建采购单、改价或发布。
4. 需要专业操作时点击“高级管理”，进入 /admin/advanced 核对事实、版本、证据和人工确认状态。
5. 需要图片客服或润色时，从统筹工作台的“图片客服”入口进入 /customer-test：选择演示商品或订单，输入顾客问题，可上传或粘贴一张图片；回复下方会标出视觉识别和润色是否实际采用。

- /admin 使用服务端持久化会话；刷新页面或切换会话时，历史记录从同一租户/管理员范围的数据库读取。
- 旧客户端仍可调用 /v1/admin/workspace/chat/stream，但它只是兼容入口，会委托持久化会话链路并忽略客户端自带历史，避免出现第二套路由语义。
- 复合查询只有在全部核实任务成功时才标记为完成；全部失败会留下可重试的未完成记录，不会把空回答写入正常历史。
- 会话标题和展示边界会在 API 写入/返回前脱敏；不要把客户姓名、电话、地址或其他个人信息主动放进标题。

## 客服 Demo 的边界

- 页面显示的商品、库存、订单和客服会话属于虚拟/隔离演示数据，不代表真实店铺实时状态。
- “已润色”只表示在事实核验后的回复上完成受控文字整理，不改变价格、库存、物流、退款或售后事实。
- “已识图”只表示视觉模型返回了可用观察结果；无法确认的订单或商品信息仍会保持不确定，不会由图片猜测。
- 统筹 Agent 的写操作只返回确认建议；当前 Demo 不执行退款、赔付、改价、采购、付款、发布或平台消息发送。

## 部署步骤（服务器）

以下步骤假定服务目录为 /opt/yunpai-ecommerce-agent，进程由 systemd 管理，公网由 Nginx 反向代理。实际主机名、证书和备份目录以服务器现有配置为准。

1. 拉取已审核的 GitHub 分支，并在停机窗口前确认工作区干净。
2. 在服务器私有环境文件中配置模型、认证和数据目录。多模态能力对应 VISION_*，润色能力对应 POLISH_*；真实密钥只放在权限为 0600 的环境文件或密钥管理器中。
3. 停止服务并按现有灾备流程创建、验证当前 schema 的加密备份；不要直接覆盖旧备份。
4. 更新代码和虚拟环境，执行数据库初始化或前向迁移；仅在需要演示数据时执行 simulate-store --load-only。
5. 启动服务，依次检查 /health、/ready、/admin、/admin/advanced 和 /customer-test。
6. 通过前置认证做一次商品、库存、订单和图片客服请求；确认 /health 中的视觉和润色状态与实际配置一致。
7. 保留切换前备份和上一版源码归档。若健康检查失败，停止新版本并按现有回滚流程恢复，不在公网临时关闭认证。

## 本地交接/复现

使用 Python 3.11+ 和仓库虚拟环境。典型的离线 smoke 命令如下：

    python3.11 -m venv .venv
    .venv/bin/python -m pip install -e .[dev]
    export DATA_DIR=$PWD/data-workspace-agent
    export ADMIN_AUTH_REQUIRED=true
    export AUTH_REQUIRED=true
    export MODEL_ENABLED=false
    export MODEL_MOCK_MODE=true
    export CUSTOMER_TEST_ENABLED=true
    .venv/bin/python -m ecommerce_agent.cli init
    .venv/bin/python -m ecommerce_agent.cli simulate-store --load-only
    .venv/bin/python -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8091

然后打开 http://127.0.0.1:8091/admin；高级后台是 http://127.0.0.1:8091/admin/advanced。要验证真实视觉和润色服务，将模型配置放在本地私有环境中，并先用 model-probe 检查连通性。

## 验收重点

- /admin 与 /admin/advanced 是同一服务的两个页面，不要为统筹 Agent 另起一套 API 或数据库。
- 统筹工具目录应能看到预测、库存计划和生命周期建议，并显示中文业务标签。
- /customer-test 的图片请求应展示视觉状态、媒体历史和润色状态；失败时保留原回复并明确标注回退。
- 任何平台写动作、真实渠道消息发送和真实经营结论都不由本 Demo 的本机或公网测试结果放行。
