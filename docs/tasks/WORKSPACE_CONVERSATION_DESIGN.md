# 统筹 Agent 会话持久化设计

## 目标

一期面向单负责人，保存统筹 Agent 的会话、消息和处理过程。页面刷新、服务重启后仍可恢复；不同会话的上下文互不污染。

## 范围

- 最近 20 个会话，按最后更新时间倒序展示。
- 新建、重命名、归档会话；一期不做永久删除。
- 首条问题生成确定性标题，不额外调用模型。
- 保存用户消息、Agent 回答、生成状态、时间、工具名称、核对摘要、确认卡片和 trace ID。
- 页面刷新优先恢复浏览器记录的当前会话；该会话不可用时恢复最近活跃会话。
- 后端从当前会话加载最近 12 条已完成消息作为模型上下文，前端不再提交可信 history。
- 流式请求开始时保存用户消息和 `generating` 助手占位；正常结束改为 `completed`，中断或异常改为 `incomplete`。
- 所有入库文本先经过现有脱敏逻辑。

## 非目标

- 不与顾客客服会话共用表、接口或列表。
- 不做多人共享、会话转移、协同编辑、标签、文件夹或全文搜索。
- 不从历史会话自动提取长期偏好或长期记忆。
- 不改变统筹 Agent 的工具权限、写操作门禁或业务模块状态。

## 数据模型

新增独立表 `workspace_conversations`：

- `id`：服务端生成的会话 ID，格式 `workspace:<uuid>`。
- `tenant_id + admin_id`：一期的负责人归属和未来扩展边界。
- `title`：首条问题前 20 个归一化字符，支持人工修改。
- `status`：`active | archived`。
- `created_at`、`updated_at`。

新增独立表 `workspace_messages`：

- `id`、`conversation_id`、`tenant_id`、`admin_id`。
- `role`：`user | assistant`。
- `content`。
- `status`：`completed | generating | incomplete`。
- `trace_id`、`tool_name`、`tool_label`、`tool_summary`。
- `requires_confirmation`、`action_summary`。
- `created_at`、`updated_at`。

消息通过复合归属校验关联会话；跨 tenant 或 admin 的访问统一返回 404，避免泄露存在性。

## API

- `POST /v1/admin/workspace/conversations`
- `GET /v1/admin/workspace/conversations?limit=20`
- `GET /v1/admin/workspace/conversations/{id}/messages`
- `PATCH /v1/admin/workspace/conversations/{id}`
- `POST /v1/admin/workspace/conversations/{id}/chat/stream`

流式接口不再接受前端提供的 `session_id` 或 `history`。服务端以路径中的会话 ID解析归属并装配最近历史。

## 页面行为

- 左侧会话栏显示新建按钮和最近会话。
- 刷新时恢复当前会话的完整消息及处理过程。
- 没有会话时创建首个会话；选择新建不会删除旧会话。
- `generating` 在恢复时显示“回答生成中”；服务启动时将遗留的 `generating` 统一收敛为 `incomplete`。
- 归档当前会话后切换到最近活跃会话；没有活跃会话时新建空会话。

## 验收

1. 一轮问答后刷新，问题、回答和处理过程完整恢复。
2. 三轮问答后刷新，顺序与上下文保持一致。
3. 新建第二个会话后可以切回第一个，且上下文互不污染。
4. 流式回答中断后恢复为“回答未完成”。
5. 退出登录不删除历史，重新认证后才可读取。
6. 跨管理员或跨租户访问返回 404。
7. 服务重启后会话仍存在。

## 门禁

本设计需要新增数据库表。必须先由负责人登记一个新的 Schema 版本号；不得占用已经预留给 M6-R 的 29 或 30，也不得在初始化流程之外临时建表。
