# 统筹 Agent 工作台（新前端）交接与启动说明

> 分支：`feature/workspace-agent-pr11-pr12`（在你 fork `a1024053774/yunpai-ecommerce-agent` 上）。
> 内容 = PR #11（会话历史持久化）+ PR #12（复合只读查询）+ 两轮独立修复，已解决互相冲突并全量测试。
> 你的 `main` 未动（仍为 `454b35c` / v35 / M7-R WP5）；PR #11 / #12 保持 Draft 原样，后续开发请直接在这个新分支上继续。

## 1. 环境变量

沿用根目录 `env.md` 的导出块（同一个模型与密钥配置），无新增必需变量。
与工作台直接相关的只有这几项：

```bash
export BOOTSTRAP_ADMIN_ID="local-admin"
export ADMIN_API_KEY="<与 env.md 相同的管理员密钥>"
export MODEL_PROVIDER="deepseek"        # 统筹 Agent 的规划/回答模型，必须可用
export MODEL_ENABLED="true"
export DATA_DIR="./data"
```

注意：不配置可用模型时，工作台的规划轮会 fail-closed 返回 `planning_failed`
错误（安全，不会执行任何写操作），但正常体验需要真实模型。

## 2. 启动

```bash
git fetch origin && git checkout feature/workspace-agent-pr11-pr12
pip install -e .

# 复制 env.md 的 export 块到当前 shell 后：
yunpai-agent init &&
yunpai-agent eval &&
yunpai-agent simulate-store &&
yunpai-agent serve --host 127.0.0.1 --port 8080
```

数据库兼容：老 `main`（v35）的数据目录可直接打开，启动时自动补齐 v31
工作台会话表，不动 v32–v35 的既有表，版本号不回退。

## 3. 页面入口

- 新前端（统筹工作台）：`http://127.0.0.1:8080/admin`
  首次打开在页面里填 `BOOTSTRAP_ADMIN_ID` + `ADMIN_API_KEY` 登录。
- 旧管理控制台：`http://127.0.0.1:8080/admin/advanced`（原 `/admin` 的全部功能原样保留）

## 4. 接口速查（`/v1/admin/workspace`，均需 `X-Admin-Id` / `X-Admin-Key` 头）

```bash
# 建会话
curl -X POST http://127.0.0.1:8080/v1/admin/workspace/conversations \
  -H "X-Admin-Id: local-admin" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"title":"冒烟"}'

# 会话内流式对话（SSE）
curl -N -X POST http://127.0.0.1:8080/v1/admin/workspace/conversations/{id}/chat/stream \
  -H "X-Admin-Id: local-admin" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"查看库存风险和最近收入。","context":{}}'

# 无会话一次性对话 / 历史会话列表 / 消息列表 / 能力目录
POST /v1/admin/workspace/chat/stream
GET  /v1/admin/workspace/conversations
GET  /v1/admin/workspace/conversations/{id}/messages
GET  /v1/admin/workspace/capabilities
```

## 5. 功能范围（测试重点）

- 会话历史：跨刷新持久化；消息的 `trace_id / tool_name / requires_confirmation`
  等元数据落在专用列；部分更新不误清字段。
- 复合只读查询：一次规划产出多个只读子目标，并发上限 3，共享
  20s/90s 超时预算；依赖链用显式 `argument_refs`（`task_id + JSON 路径`），
  缺失/越界 fail closed。
- 写操作（退款、采购、发布等）：统一转确认卡（`requires_confirmation=true`），
  当前统筹入口不生成、不提交、不执行任何写动作，确认后请进 `/admin/advanced` 对应模块。

## 6. 已知开放问题（交测时留意，未定论）

- 模型不可用时，明确写请求返回 `planning_failed` 错误而非确认卡
  （fail-closed，安全）。`workspace_agent.py` 里的
  `_requires_confirmation_request` 目前是死代码（定义了未接线），
  是接成"无模型也返回确认卡"还是删除，留给后续在新分支上决定。
- 未经真实模型 benchmark、真实平台数据与长稳验证；pytest 全量为单元/集成级。

## 7. 回归验证

```bash
python -m compileall -q src tests && python -m pytest -q
```

预期全绿（约 1100+ 项；`/admin` 结构断言已随本分支指向 `/admin/advanced`）。
