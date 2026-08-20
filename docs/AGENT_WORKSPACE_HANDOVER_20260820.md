# 统筹 Agent 统一前端交测交接（2026-08-20）

本文用于把老板在 PR #9 提交的统一前端，以及其后的会话持久化、复合只读查询和独立修复，交给下一位测试或开发同事。本文只记录已核验的代码事实，不把 PR 描述、已有 `passed` 截图或旧门禁当作本轮结论。

## 1. 交付对象

- 仓库：`a1024053774/yunpai-ecommerce-agent`
- 交测分支：`feature/workspace-agent-pr11-pr12`
- 功能合成提交：`921e676`
- 当前 fork `main`：`454b35c`，本轮没有修改或推送 `main`
- PR #9 基线提交：`adee44e`
- PR #11 已核验 head：`6ec6a89`
- PR #12 已核验 head：`68e3846`

`adee44e`、`6ec6a89`、`68e3846` 都是交测分支的祖先。分支 head 还会包含本文和验收台账提交；要核对当前精确值请运行：

```bash
git rev-parse HEAD
git merge-base --is-ancestor adee44e HEAD
git merge-base --is-ancestor 6ec6a89 HEAD
git merge-base --is-ancestor 68e3846 HEAD
```

截至 2026-08-20 的 GitHub 状态：

- PR #9 已恢复为 Open + Draft，代码未丢失；它与当前 `main` 显示 `CONFLICTING`，不能直接按旧 head 无脑合并。
- PR #11、PR #12 均保持 Open + Draft，分别对 `main` 显示 `MERGEABLE`。这只表示各自对 `main` 的状态，不表示两者顺序合并一定无冲突。
- 两个 Draft PR 的冲突已经在交测分支的 `921e676` 中显式解决，因此外部测试应优先测试该分支，不要分别拼接三个 PR。

## 2. PR #9 是什么

PR #9 相对它的 merge-base `4598fe0` 只有两个功能提交：

1. `5ea1744 feat: add unified agent workspace`
2. `adee44e fix: improve workspace routing resilience`

该 PR 的真实差异是 20 个文件、3985 行新增、65 行删除，核心交付为：

- 新增统一工作台页面 `docs/agent-workspace.html`。
- `/admin` 改为统筹 Agent 统一入口。
- 原高级管理控制台迁移到 `/admin/advanced`，原功能继续保留。
- 新增 `workspace_agent.py`、`workspace_api.py`、`workspace_presenter.py`。
- 接入工作台 API、受控工具目录、流式响应、确认卡与对应测试。

注意：PR #9 之后分支同步了大量 `main` 上的 M7-R、知识库、预测等提交。这些是同步得到的主线能力，不应写成“PR #9 新增了这些功能”。

## 3. 基于 PR #9 新增的功能

### 3.1 PR #11：持久会话历史

- 新增 `workspace_conversations` 和 `workspace_messages`，按 tenant、管理员和会话隔离。
- 支持创建、列表、重命名、归档会话，以及读取消息历史。
- 会话流式接口由服务端加载最近 12 条已完成消息；前端不能提交可信历史覆盖服务端状态。
- `/admin` 增加历史会话列表和“新建会话”，刷新或重启后仍可恢复。
- 消息记录保留 `trace_id`、工具名/标签/摘要、确认状态、动作摘要和处理状态。
- 用户输入和回复落库前执行敏感信息脱敏；中断或异常生成会保留为 `incomplete`，陈旧的 `generating` 状态可恢复。

相关 API：

```text
POST  /v1/admin/workspace/conversations
GET   /v1/admin/workspace/conversations
PATCH /v1/admin/workspace/conversations/{conversation_id}
GET   /v1/admin/workspace/conversations/{conversation_id}/messages
POST  /v1/admin/workspace/conversations/{conversation_id}/chat/stream
```

### 3.2 PR #12：复合只读查询

- 模型可一次规划多个只读子任务，并汇总全部成功、无数据、失败或跳过结果。
- 单个计划最多 4 个任务；并发上限 3。
- 单任务预算 20 秒；整个计划共享 90 秒墙钟预算。
- 支持显式依赖图，拒绝未知依赖、自依赖、循环依赖和未声明引用。
- 后置任务通过 `argument_refs` 从前置任务结构化结果取参数，格式为 `task_id + JSON path`。
- 缺失路径、越界路径或前置失败会 fail closed，不退化成无筛选的宽范围查询。
- 复合计划只允许调用能力目录中的只读工具；写请求仍由模型返回 `propose_action`，当前统筹入口不直接执行。

示意结构：

```json
{
  "tasks": [
    {
      "task_id": "inventory",
      "objective": "查库存风险",
      "tool_name": "get_inventory_risk",
      "arguments": {},
      "argument_refs": {},
      "depends_on": []
    },
    {
      "task_id": "product",
      "objective": "补充风险商品事实",
      "tool_name": "get_product_facts",
      "arguments": {},
      "argument_refs": {
        "sku_id": {"task_id": "inventory", "path": ["risks", 0, "sku_id"]}
      },
      "depends_on": ["inventory"]
    }
  ]
}
```

## 4. 基于 PR #9 修复的 bug

| 问题 | 当前修复 | 交测关注点 |
|---|---|---|
| 消息 metadata 只在临时字典中，专用列写入或读取会丢失 | 数据库、API、展示层统一恢复六个结构化字段 | 发一轮带工具/确认信息的消息，刷新后字段仍在 |
| 部分更新会误清未提供字段；显式 `None` 又不能只清目标字段 | 区分“字段未传”与“字段显式传空” | 更新一个字段时其他 metadata 不变；显式空值只清该字段 |
| 确定性关键词/正则会覆盖模型的 `answer`、`clarify` 或 `propose_action` | 删除语义路由覆盖，只保留可验证执行安全边界 | 假设、售前和否定问句不能因出现“退款/采购”等词被强制转确认卡 |
| `clarify` 可能原样承诺“确认后生成、提交、执行” | 澄清文案由允许的缺失项和当前能力边界生成 | 缺店铺等信息时只追问，不越权承诺动作 |
| 首轮规划提示没有要求明确写请求输出 `propose_action` | 提示契约补齐该模式及 `action_summary` | 真实模型下明确写请求应给确认卡，但不执行 |
| 前置任务证据会被混入后置工具参数，触发严格 schema 拒绝或污染调用 | 前置证据与工具参数分离 | 工具只收到 schema 声明参数，不出现内部下划线键 |
| 并发 future 按任务逐个等待，超时会被累计 | 同批任务共享一次 wait，且受整计划 deadline 限制 | 多个慢任务的墙钟时间接近一次预算，不是任务数乘预算 |
| 依赖只有顺序，没有显式参数引用；缺路径时可能变成宽查询 | 新增 `argument_refs`，引用必须来自已声明前置任务 | 缺失或越界只失败相关任务，不能去查全量数据 |
| 消息 limit 曾返回较旧记录，时间相同的顺序不稳定 | 按插入顺序稳定返回最新窗口 | 超过 limit 后仍得到最新消息且显示顺序正确 |
| 新前端将旧控制台迁到 `/admin/advanced`，M7-R readiness 测试仍访问 `/admin` | PR #11/#12 各补一项路径修复 | `/admin` 是统筹工作台；`/admin/advanced` 含数据准备度等旧控制台功能 |

准确边界：直接向旧运行时 stub 注入 `propose_action` 时，旧运行时本来就能保留该决定。本轮修的是“首轮提示要求真实模型产出该结构”以及相邻安全边界，不应表述成“旧运行时一定会吞掉 propose_action”。

## 5. 本轮独立核验结果

以下结果均在交测分支上重新运行，不引用 Claude 或 PR 正文里的旧 `passed` 数字。

### 5.1 独立反例探针

最终合成代码的七组探针全部退出 0：

```text
metadata: ok
semantic_answer: ok
clarification_copy: ok
propose_action: ok
propose_prompt_contract: ok
timeout: ok (0.086s)
argument_refs: ok
```

旧 head 的独立失败证据包括：

- PR #11 旧基线不接受消息 `metadata`；澄清文案越权承诺生成并提交采购单；假设问句中的模型 `answer` 被正则改成确认动作。
- PR #12 旧基线三个 `0.08s` 慢任务实际约 `0.405s`；不识别 `argument_refs`；首轮提示缺少 `propose_action` 结构契约。
- readiness 路径回归在坏提交上稳定失败，在 `6ec6a89`、`68e3846` 和最终合成分支上通过。

### 5.2 测试与静态检查

- 影响范围回归：`81 passed in 39.05s`。
- 仓库全量：`1115 passed in 785.87s`，退出 0。
- 全量输出没有 failed、skipped 或 xfailed 项。
- 最终文档提交前另运行 `compileall`、`git diff --check` 和 project-to-act 校验；以本分支最终提交记录为准。

影响范围命令：

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_workspace_conversations.py \
  tests/test_workspace_read_plan.py \
  tests/test_workspace_agent.py \
  tests/test_m7r_wp4_readiness.py
```

全量命令：

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

### 5.3 v35 数据迁移 smoke

用 `main@454b35c` 创建 schema v35 数据目录，再由交测分支原地打开：

- schema 保持 v35，不回退。
- 原有 156 条知识和 2 条 SOP 定义保留。
- 自动补齐 `workspace_conversations`、`workspace_messages`。
- 自动补齐六个消息 metadata 专用列。
- `schema_migrations` 从 34 条变为 35 条。

`.yunpai-runtime.lock` 文件可能保留；锁是否释放应以服务停止后同目录可以再次 `init` 为准，不以锁文件是否被删除判断。本轮同目录重新初始化成功。

### 5.4 HTTP 与真实浏览器 smoke

全新临时数据目录、真实 uvicorn、离线 mock 模型下：

```text
GET  /health                                          200, schema=35, model=mock
GET  /admin                                           200, 统一工作台
GET  /admin/advanced                                  200, 原高级控制台
POST /v1/admin/workspace/conversations                201
GET  /v1/admin/workspace/conversations                200
GET  /v1/admin/workspace/conversations/{id}/messages  200
GET  /v1/admin/workspace/capabilities                 200, automatic_writes=false, tools=21
```

浏览器实际完成管理员登录、新建会话、发送消息、恢复 2 条历史消息、打开 `/admin/advanced`；两个页面的控制台 error/warning 均为 0。离线 mock 对经营问题明确返回 `planning_failed`，没有伪造计划或执行成功。非阻断项：浏览器会请求 `/favicon.ico`，当前服务返回 404，不影响页面和 API。

## 6. 第一次启动：离线交互 smoke

需要 Python 3.11 以上。以下示例只使用占位符；真实密钥不得粘贴到本文、Issue、PR 评论或提交中。

```bash
git fetch origin
git switch --track origin/feature/workspace-agent-pr11-pr12

python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

若本地分支已经存在，使用：

```bash
git switch feature/workspace-agent-pr11-pr12
git pull --ff-only
```

本次交测另提供统一工作台专属启动文件：

- 仓库内的 `workspace-env.example.md` 是无密钥模板，可随分支交接。
- 维护者本机的 `workspace-env.md` 已复制现有真实配置，并加入当前工作台需要显式设置的模型与会话参数；它被 `.gitignore` 排除，只能通过批准的安全渠道单独交付。
- 专属文件使用 `DATA_DIR=$PWD/data-workspace-agent` 和端口 `8091`，避免与主工作区的 `8080`、PR #9 工作区的 `8090` 相互污染或抢占端口。
- 新前端和后端仍由同一个 `serve` 进程提供；当前代码没有额外的 `WORKSPACE_*` 应用配置项。模板中的 `WORKSPACE_HOST`、`WORKSPACE_PORT` 只用于拼装启动命令。

收到私密文件时，优先从仓库根目录复制其中完整代码块执行。真实模型模式会先运行 `model-probe`，离线 mock 模式跳过探针；随后执行 `init` 并在 `8091` 启动服务。若未收到私密文件，可继续使用下面的占位符做离线交互 smoke。

设置本地测试环境。三个 secret 应使用不同的本地随机值：

```bash
export DATA_DIR="$PWD/data"
export ADMIN_AUTH_REQUIRED="true"
export BOOTSTRAP_ADMIN_ID="local-admin"
export ADMIN_API_KEY="<LOCAL_ADMIN_SECRET>"

export AUTH_REQUIRED="true"
export BOOTSTRAP_TENANT_ID="local-appliance"
export BOOTSTRAP_CLIENT_ID="local-adapter"
export BOOTSTRAP_CLIENT_KEY="<LOCAL_CLIENT_SECRET>"
export SUBJECT_HASH_KEY="<LOCAL_HMAC_SECRET>"

export MODEL_ENABLED="false"
export MODEL_MOCK_MODE="true"
```

初始化并启动：

```bash
PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli init
PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8080
```

打开：

- `http://127.0.0.1:8080/admin`：统筹 Agent 统一工作台。
- `http://127.0.0.1:8080/admin/advanced`：原高级管理控制台。

`eval` 和 `simulate-store` 都不是启动前置。需要额外验收时再单独运行；`simulate-store` 会向当前 `DATA_DIR` 写入显式虚拟数据，建议使用单独测试目录。

## 7. 接入真实模型

离线 mock 只验证页面、API、持久化和安全失败路径，不能验收语义质量。要测试统筹规划和复合查询，请在私有 shell 或密钥管理器中设置以下变量：

```bash
export MODEL_MOCK_MODE="false"
export MODEL_ENABLED="true"
export MODEL_PROVIDER="<provider>"
export MODEL_BASE_URL="<standard_api_base_url>"
export MODEL_NAME="<model_name>"
export MODEL_API_KEY="<secret>"
```

先做最小连通性检查，再启动服务：

```bash
PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli model-probe
PYTHONPATH=src .venv/bin/python -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8080
```

变量的完整说明见 `.env.example`，本次交测的完整启动结构见 `workspace-env.example.md`。真实凭据只放在被忽略的本机 `workspace-env.md` 或密钥管理器中，不得进入仓库、Issue 或 PR 评论。

## 8. 交接人员自测清单

1. 登录 `/admin`，确认有“历史会话”和“新建会话”。
2. 新建会话，发送普通只读问题；刷新页面后确认用户和 Agent 消息仍在。
3. 使用真实模型发送复合只读问题，例如“查看库存风险和最近收入，并告诉我先处理什么”，确认可以拆成多个只读任务并完整汇总。
4. 发送假设或售前问句，例如“确认后退款会发生什么”，确认不会仅因包含“退款”二字被确定性代码强制改成写动作。
5. 发送明确写请求，确认返回 `propose_action` / `requires_confirmation=true`，且没有实际退款、采购、发布或改价。
6. 构造需要前置结果的查询，关注 `argument_refs` 缺失、路径越界和前置失败是否 fail closed。
7. 刷新后检查工具摘要、确认状态和动作摘要没有丢失。
8. 打开 `/admin/advanced`，确认原经营总览、数据准备度和其他高级模块仍能进入。
9. 运行上面的 81 项影响范围回归；准备合并前再运行仓库全量。

## 9. 已知限制和未覆盖 Gate

- 模型不可用或首轮输出无效时，明确写请求目前返回 `planning_failed`，而不是离线生成确认卡；这是 fail closed，不会执行写动作。
- `workspace_agent.py` 中 `_requires_confirmation_request` 已定义但未接入当前路径。本轮没有擅自决定“接线还是删除”，避免重新引入关键词语义权威；后续若修改必须遵守 D-034 并增加否定、假设、售前和复合请求反例。
- 本轮没有运行真实模型 intent benchmark，也没有验证具体供应商模型的规划质量。
- 本轮没有接入真实店铺、订单、库存、渠道或生产数据。
- 本轮没有执行 24/72 小时长稳、目标硬件容量、安全发布、灾备实操或生产 Gate。
- PR #9 已恢复，但当前仍与 `main` 冲突；PR #11/#12 仍是 Draft。
- `/favicon.ico` 404 是已观察到的非阻断前端资源缺口。

因此当前结论是“可以交给别人做功能和集成测试”，不是“可以直接合并 main”或“可以生产发布”。

## 10. 后续合并建议

1. 测试员以 `feature/workspace-agent-pr11-pr12` 为唯一交测对象。
2. 缺陷修复继续提交到该分支或从其创建短分支，避免再分别修改 #9/#11/#12 后重复解冲突。
3. 外部测试通过后，从该交测分支向 fork `main` 新开一个合并 PR。
4. 合并前刷新 `main`，重跑影响范围、全量、真实模型 smoke 和必要的迁移验证。
5. #9 是老板的原始统一前端 PR，应保留可追溯性；最终如何关闭或标记被组合 PR 取代，由仓库负责人决定，不再误删。
