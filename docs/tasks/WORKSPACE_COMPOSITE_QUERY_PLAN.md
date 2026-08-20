# 统筹 Agent 复合只读查询实施计划

> 实施时必须按任务顺序执行，每项先写失败测试、确认旧实现失败、完成最小实现、复验并独立提交。

**目标：** 将统筹 Agent 从逐轮临时规划升级为一次生成只读子目标清单，按依赖分批、每批最多三个并发执行，并可靠汇总每个子目标的核实结果。

**架构：** 新建独立的 `workspace_read_plan.py` 承载计划/结果类型、计划校验和依赖批次调度，避免继续扩大 `workspace_agent.py`。`WorkspaceAgent` 负责调用一次规划模型、通过现有工具注册表执行只读任务、将结果交给现有 presenter，再以确定性覆盖检查约束最终回答。业务工具、权限策略和数据库不变。

**技术栈：** Python 3.11、Pydantic v2、`concurrent.futures.ThreadPoolExecutor`、FastAPI SSE、pytest；不新增第三方依赖，不新增 Schema。

## 全局约束

- 基线固定为 PR #9 提交 `adee44e`；正式 PR 明确依赖 PR #9。
- 只允许动态目录中 `kind=read` 的工具；`kind=generate` 和业务写动作不进入并发执行器。
- 一个计划最多四个任务，同一批并发上限固定为 `3`。
- 无依赖任务可并发；依赖任务只能在全部前置任务成功后执行。
- 继续执行 D-015 的订单 `order_id + shop_id` 可信范围门禁和 D-034 的模型语义路由原则。
- 不新增关键词到工具的硬映射，不修改各业务模块的数据口径。
- 每个子目标必须保留独立的 `success | no_data | failed | skipped` 状态。
- “无数据”不得转换为数值零；有证据且计算值为零时必须保留真实零值。
- 最终回答必须覆盖所有明确子目标，并保持核实数字、金额、比例和业务标识符原样。
- 不修改 `.project-to-act`；只有产生新鲜验收证据后，才按项目规则请求负责人更新规范账本。

---

## 文件结构

- 新建 `src/ecommerce_agent/workspace_read_plan.py`
  - 只负责计划、任务、结果类型，计划校验，拓扑批次和并发执行。
- 修改 `src/ecommerce_agent/workspace_agent.py`
  - 只负责模型规划、现有工具执行接线、SSE 事件、回答生成和写门禁。
- 修改 `src/ecommerce_agent/workspace_presenter.py`
  - 为产品化结果增加明确数据状态和可追溯关键值，不改变领域计算。
- 新建 `tests/test_workspace_read_plan.py`
  - 纯单元测试覆盖无环依赖、去重、批次、并发上限和失败隔离。
- 修改 `tests/test_workspace_agent.py`
  - 覆盖真实库存+收入、部分失败、三/四任务、写门禁和回答数字守卫。
- 修改 `tests/test_workspace_presenter.py`
  - 覆盖无数据与真实零值的产品语言契约。

---

### 任务 1：定义只读计划与依赖批次

**文件：**

- 新建：`src/ecommerce_agent/workspace_read_plan.py`
- 新建：`tests/test_workspace_read_plan.py`

**接口：**

```python
class WorkspaceReadTask(BaseModel):
    task_id: str
    objective: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

class WorkspaceReadPlan(BaseModel):
    response: str | None = None
    tasks: list[WorkspaceReadTask] = Field(default_factory=list)

class WorkspaceTaskResult(BaseModel):
    task_id: str
    objective: str
    tool_name: str
    tool_label: str
    status: Literal["success", "no_data", "failed", "skipped"]
    verified_facts: list[str] = Field(default_factory=list)
    critical_values: list[str] = Field(default_factory=list)
    error_summary: str | None = None
    data_as_of: str | None = None

def validate_read_plan(
    plan: WorkspaceReadPlan,
    *,
    readable_tools: set[str],
    maximum_tasks: int = 4,
) -> WorkspaceReadPlan

def ready_task_batches(plan: WorkspaceReadPlan, *, batch_size: int = 3) -> list[list[str]]
```

- [x] **步骤 1：写计划校验失败测试**

在 `tests/test_workspace_read_plan.py` 固定以下用例：

```python
def test_read_plan_rejects_unknown_non_read_and_cyclic_tasks() -> None:
    unknown = WorkspaceReadPlan(tasks=[
        WorkspaceReadTask(
            task_id="inventory",
            objective="核对库存",
            tool_name="write_inventory",
        )
    ])
    with pytest.raises(ValueError, match="read_tool_not_allowed"):
        validate_read_plan(unknown, readable_tools={"get_inventory_risk"})

    cyclic = WorkspaceReadPlan(tasks=[
        WorkspaceReadTask(
            task_id="a", objective="A", tool_name="get_inventory_risk", depends_on=["b"]
        ),
        WorkspaceReadTask(
            task_id="b", objective="B", tool_name="get_business_metric", depends_on=["a"]
        ),
    ])
    with pytest.raises(ValueError, match="read_plan_cycle"):
        validate_read_plan(
            cyclic,
            readable_tools={"get_inventory_risk", "get_business_metric"},
        )
```

同时覆盖任务数大于 4、重复 task_id、未知依赖、自依赖和空任务计划。

- [x] **步骤 2：运行红灯**

运行：

```powershell
python -m pytest -q tests/test_workspace_read_plan.py
```

预期：收集阶段因 `workspace_read_plan` 不存在而失败。

- [x] **步骤 3：实现 Pydantic 类型与计划校验**

实现以下确定性规则：

```python
if not plan.tasks:
    raise ValueError("read_plan_empty")
if len(plan.tasks) > maximum_tasks:
    raise ValueError("read_plan_too_large")
if task.tool_name not in readable_tools:
    raise ValueError("read_tool_not_allowed")
```

用 DFS 的 `visiting/visited` 集合检查循环，不根据 objective 内容判断工具。

- [x] **步骤 4：写并通过批次测试**

```python
def test_ready_task_batches_caps_independent_queries_at_three() -> None:
    plan = WorkspaceReadPlan(tasks=[
        WorkspaceReadTask(task_id="a", objective="A", tool_name="read_a"),
        WorkspaceReadTask(task_id="b", objective="B", tool_name="read_b"),
        WorkspaceReadTask(task_id="c", objective="C", tool_name="read_c"),
        WorkspaceReadTask(task_id="d", objective="D", tool_name="read_d"),
    ])
    assert ready_task_batches(plan, batch_size=3) == [["a", "b", "c"], ["d"]]


def test_ready_task_batches_waits_for_dependencies() -> None:
    plan = WorkspaceReadPlan(tasks=[
        WorkspaceReadTask(task_id="search", objective="找商品", tool_name="search_products"),
        WorkspaceReadTask(
            task_id="competitor",
            objective="查竞品",
            tool_name="get_competitive_intelligence",
            depends_on=["search"],
        ),
    ])
    assert ready_task_batches(plan, batch_size=3) == [["search"], ["competitor"]]
```

运行：

```powershell
python -m pytest -q tests/test_workspace_read_plan.py
```

预期：全部通过。

- [x] **步骤 5：提交任务 1**

```powershell
git add src/ecommerce_agent/workspace_read_plan.py tests/test_workspace_read_plan.py
git commit -m "feat: define workspace read task plans"
```

---

### 任务 2：实现依赖感知的并发只读执行器

**文件：**

- 修改：`src/ecommerce_agent/workspace_read_plan.py`
- 修改：`tests/test_workspace_read_plan.py`

**接口：**

```python
ReadTaskRunner = Callable[[WorkspaceReadTask], WorkspaceTaskResult]

def execute_read_plan(
    plan: WorkspaceReadPlan,
    *,
    runner: ReadTaskRunner,
    maximum_parallel: int = 3,
) -> list[WorkspaceTaskResult]
```

- [x] **步骤 1：写并发峰值与顺序红灯测试**

使用 `threading.Barrier` 和加锁计数器记录同时运行数量，不使用 `sleep` 判断并发：

```python
def test_execute_read_plan_runs_three_independent_tasks_together() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def runner(task: WorkspaceReadTask) -> WorkspaceTaskResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return successful(task)

    results = execute_read_plan(three_task_plan(), runner=runner, maximum_parallel=3)
    assert peak == 3
    assert [item.task_id for item in results] == ["a", "b", "c"]
```

另写四任务测试，断言峰值仍为 3 且返回顺序与原计划一致。

- [x] **步骤 2：运行红灯**

运行：

```powershell
python -m pytest -q tests/test_workspace_read_plan.py -k "execute_read_plan"
```

预期：因 `execute_read_plan` 未定义而失败。

- [x] **步骤 3：实现批次并发执行**

每个批次使用：

```python
with ThreadPoolExecutor(max_workers=min(maximum_parallel, len(batch))) as pool:
    futures = {pool.submit(runner, task): task for task in batch}
```

捕获每个 future 的异常并转换为该任务的 `failed` 结果，不从工作线程传播到整个请求。完成后按原计划 task_id 顺序返回结果。

- [x] **步骤 4：实现依赖失败跳过**

若任一依赖结果不是 `success`，后置任务不调用 runner，直接生成：

```python
WorkspaceTaskResult(
    task_id=task.task_id,
    objective=task.objective,
    tool_name=task.tool_name,
    tool_label="业务信息",
    status="skipped",
    error_summary="前置信息未核实，未继续查询",
)
```

测试必须断言 runner 没有收到后置 task_id。

- [x] **步骤 5：运行任务 2 全部测试**

```powershell
python -m pytest -q tests/test_workspace_read_plan.py
```

预期：全部通过，无超时、无后台线程残留。

- [x] **步骤 6：提交任务 2**

```powershell
git add src/ecommerce_agent/workspace_read_plan.py tests/test_workspace_read_plan.py
git commit -m "feat: execute independent workspace reads concurrently"
```

---

### 任务 3：接入一次性规划和工作台 SSE

**文件：**

- 修改：`src/ecommerce_agent/workspace_agent.py`
- 修改：`tests/test_workspace_agent.py`

**接口：**

```python
def WorkspaceAgent._read_plan(self, request: WorkspaceChatRequest) -> WorkspaceReadPlan

def WorkspaceAgent._run_read_task(
    self,
    task: WorkspaceReadTask,
    request: WorkspaceChatRequest,
    admin: AdminPrincipal,
    trace_id: str,
) -> WorkspaceTaskResult
```

- [x] **步骤 1：写真实“库存 + 收入”红灯测试**

在 fixture 中通过现有领域服务写入 `qingchuan-flagship-001` 的 10 条库存记录和已支付订单，使库存高风险数为 4、收入为 `4181.00`。模型只返回一次计划：

```python
{
    "tasks": [
        {
            "task_id": "inventory",
            "objective": "核对库存风险",
            "tool_name": "get_inventory_risk",
            "arguments": {"store_id": "qingchuan-flagship-001"},
            "depends_on": [],
        },
        {
            "task_id": "revenue",
            "objective": "核对已支付收入",
            "tool_name": "get_business_metric",
            "arguments": {
                "metric": "gross_revenue",
                "store_id": "qingchuan-flagship-001",
            },
            "depends_on": [],
        },
    ]
}
```

断言：

```python
assert generate_json.call_count == 1
assert [item["tool_name"] for item in done["tools_used"]] == [
    "get_inventory_risk",
    "get_business_metric",
]
assert done["completion_status"] == "completed"
assert "10" in done["answer"]
assert "4" in done["answer"]
assert "4181.00" in done["answer"]
```

- [x] **步骤 2：运行旧实现红灯并保存原因**

```powershell
python -m pytest -q tests/test_workspace_agent.py -k "composite_inventory_and_revenue"
```

预期：旧 `WorkspacePlan` 不接受 `tasks`，或旧循环需要多次规划，测试失败。这是人工报告的自动化复现证据。

- [x] **步骤 3：新增只读计划 Prompt**

将 `WORKSPACE_PROMPT_VERSION` 升为 `workspace-router-v4`。规划 Prompt 明确：

- 直接回答时返回 `response` 且 `tasks=[]`。
- 实时事实问题一次列出所有必要只读任务。
- 依赖只用 task_id 表达。
- 写请求不生成 read plan，继续交给现有动作确认判断。

可读工具集合从 `tool_catalog()` 中 `kind == "read"` 动态取得，不能建立固定业务关键词表。

- [x] **步骤 4：接入并发执行器**

`stream()` 新路径：

```text
accepted -> planning -> planned -> observing(batch/task events)
-> composing -> done
```

每个任务仍发独立 `tool` SSE 事件，并增加 `task_id/objective/status`。`done.response` additive 增加：

```python
"completion_status": "completed" | "partial",
"task_results": [
    {
        "task_id": result.task_id,
        "objective": result.objective,
        "status": result.status,
        "tool_label": result.tool_label,
        "error_summary": result.error_summary,
    }
],
```

保留已有 `tools_used`、`trace_id`、`requires_confirmation` 等字段，避免页面和 PR #11 持久化接线回归。

- [x] **步骤 5：保留写门禁和直接回答路径**

在调用 `_read_plan` 前继续使用 `_requires_confirmation_request(request.message)`：明确业务写请求直接生成现有确认响应，不提交到线程池。直接回答计划不执行工具。

复跑：

```powershell
python -m pytest -q tests/test_workspace_agent.py -k "write or direct_answer or composite_inventory_and_revenue"
```

预期：全部通过。

- [x] **步骤 6：提交任务 3**

```powershell
git add src/ecommerce_agent/workspace_agent.py tests/test_workspace_agent.py
git commit -m "feat: plan composite workspace reads once"
```

---

### 任务 4：固化无数据、部分失败与核实数字守卫

**文件：**

- 修改：`src/ecommerce_agent/workspace_presenter.py`
- 修改：`src/ecommerce_agent/workspace_agent.py`
- 修改：`tests/test_workspace_presenter.py`
- 修改：`tests/test_workspace_agent.py`

**接口：**

```python
def observation_data_status(tool_name: str, observation: dict[str, Any]) -> Literal["success", "no_data"]

def critical_fact_values(product_view: dict[str, Any]) -> list[str]

def answer_preserves_critical_values(answer: str, results: list[WorkspaceTaskResult]) -> bool
```

- [x] **步骤 1：写无数据与真实零值红灯测试**

```python
def test_metric_presenter_distinguishes_no_data_from_verified_zero() -> None:
    no_data = {"display_name": "已支付且未取消订单金额", "value": "0.00", "unit": "currency", "quality": "no_data", "evidence_count": 0}
    verified_zero = {"display_name": "已支付且未取消订单金额", "value": "0.00", "unit": "currency", "quality": "available", "evidence_count": 1}

    assert observation_data_status("get_business_metric", no_data) == "no_data"
    assert "暂无数据" in present_observation("get_business_metric", no_data)["已核实信息"][0]
    assert observation_data_status("get_business_metric", verified_zero) == "success"
    assert "0.00 元" in present_observation("get_business_metric", verified_zero)["已核实信息"][0]
```

库存空列表同样判 `no_data`，而存在库存记录且建议补货为 0 仍判 `success`。

- [x] **步骤 2：运行红灯**

```powershell
python -m pytest -q tests/test_workspace_presenter.py -k "no_data or verified_zero"
```

预期：旧 presenter 将两者都表达为零，测试失败。

- [x] **步骤 3：实现数据状态和关键值提取**

产品化结果增加内部使用的 `data_status` 和 `critical_values`；传给最终模型时仅提供产品语言和关键值约束，不暴露原始字段。关键值包括：

- 金额与比例的完整字符串，如 `4181.00`。
- 计数，如库存记录 `10`、优先关注 `4`。
- SKU、订单号和店铺号等原样标识符。

- [x] **步骤 4：写部分失败测试**

让库存 runner 成功、收入 runner 抛出 `ValueError("metric_source_unavailable")`，断言：

```python
assert done["completion_status"] == "partial"
assert done["task_results"][0]["status"] == "success"
assert done["task_results"][1]["status"] == "failed"
assert "库存" in done["answer"]
assert "收入" in done["answer"]
assert "0" not in failed_section(done["answer"])
```

- [x] **步骤 5：写数字篡改反证测试**

让回答模型返回“收入 4811.00 元”。断言最终回答不包含 `4811.00`，而是切换到确定性摘要并包含 `4181.00`。守卫只检查来自核实结果的完整关键值集合，不从自然语言重新计算业务数字。

- [x] **步骤 6：实现按子目标确定性摘要**

替换旧 `_deterministic_answer` 的全局三条截断：每个任务至少取第一条核实事实；剩余事实按总长度上限截断，但不得删除整个子目标。失败、无数据和跳过任务各输出一条范围说明。

- [x] **步骤 7：运行任务 4 测试**

```powershell
python -m pytest -q tests/test_workspace_presenter.py tests/test_workspace_agent.py -k "no_data or verified_zero or partial or critical_values or composite"
```

预期：全部通过。

- [x] **步骤 8：提交任务 4**

```powershell
git add src/ecommerce_agent/workspace_presenter.py src/ecommerce_agent/workspace_agent.py tests/test_workspace_presenter.py tests/test_workspace_agent.py
git commit -m "fix: preserve every composite query result"
```

---

### 任务 5：依赖查询、回归、反证与 Draft PR

**文件：**

- 修改：`tests/test_workspace_read_plan.py`
- 修改：`tests/test_workspace_agent.py`
- 修改：`docs/tasks/WORKSPACE_COMPOSITE_QUERY_DESIGN.md`（仅在实现与设计发生已确认偏差时）
- 修改：`docs/tasks/WORKSPACE_COMPOSITE_QUERY_PLAN.md`（勾选实际完成项并记录新鲜测试结果）

- [x] **步骤 1：增加依赖链验收测试**

计划包含 `search_products -> get_competitive_intelligence`。断言后置任务在线程执行时间线上严格晚于前置完成；前置返回 ambiguous/no_data 时，后置状态为 `skipped` 且未执行。

- [x] **步骤 2：增加重复任务与四任务测试**

- 相同 `tool_name + normalized arguments` 只执行一次，但两个 task_id 都获得同一结果引用。
- 四个独立任务分两批完成，并发峰值不超过 3。
- 返回的 `task_results` 始终按原计划顺序排列，而不是按线程完成顺序排列。

- [x] **步骤 3：运行统筹 Agent 全部专项**

```powershell
python -m pytest -q tests/test_workspace_read_plan.py tests/test_workspace_agent.py tests/test_workspace_presenter.py
```

预期：全部通过，记录实际数量和耗时。

- [x] **步骤 4：运行相关业务回归**

```powershell
python -m pytest -q tests/test_catalog_orders_metrics.py tests/test_operations_modules.py tests/test_api.py
python -m compileall -q src
git diff --check
```

预期：全部通过；不沿用历史测试数量。

- [x] **步骤 5：执行两项反证**

反证 A：临时把 `maximum_parallel=1`，并发峰值测试必须失败；还原后复验。

反证 B：临时移除关键值守卫，回答模型把 `4181.00` 改为 `4811.00` 的测试必须失败；还原后复验。

只记录测试名称、退出状态、恢复后的提交和摘要，不保存完整模型输入或敏感业务数据。

- [x] **步骤 6：项目管理校验**

```powershell
python D:\AppData\Codex\home-config\skills\project-to-act\scripts\init_project_management.py --project-root D:\yunpai\.worktrees\workspace-composite-queries --validate
```

预期：managed 且无缺失模板。若主线账本已有其他人更新，重新读取并保留最新主线，不覆盖规范账本。

- [x] **步骤 7：提交验收证据**

```powershell
git add tests/test_workspace_read_plan.py tests/test_workspace_agent.py docs/tasks/WORKSPACE_COMPOSITE_QUERY_PLAN.md
git commit -m "test: verify composite workspace query boundaries"
```

- [x] **步骤 8：同步依赖并创建 Draft PR**

先检查 PR #9：

- 若 #9 已合并：从最新 `upstream/main` 普通合并，解决冲突后重新运行步骤 3–6。
- 若 #9 未合并：推送本分支并创建 Draft PR，PR 描述明确 `Depends on #9`，不请求合并。

PR 描述必须包含：

- 一次任务拆解和并发上限 3。
- 只读范围，不执行写操作。
- 库存+收入真实 fixture 的核实数字。
- 部分失败、无数据/零值、依赖跳过和数字防篡改契约。
- 定向与相关回归的实际测试数量。
- 两项反证过程。

### 实施证据（2026-08-13）

- 提交：`6016d49`（计划契约）、`2c76f88`（并发执行器）、`2f74282`（一次性复合规划）、`1763aff`（结果守卫）。
- 真实复现：同一店铺写入 10 条库存记录，其中 4 条高风险；已支付且未取消订单金额为 `4181.00`，规划模型只调用一次，两个工具均执行。
- 专项：`python -m pytest -q tests/test_workspace_read_plan.py tests/test_workspace_agent.py tests/test_workspace_presenter.py` -> `49 passed in 187.78s`。
- 相关回归：`python -m pytest -q tests/test_catalog_orders_metrics.py tests/test_operations_modules.py tests/test_api.py` -> `16 passed in 106.15s`。
- 静态验证：`python -m compileall -q src` 与 `git diff --check` 退出码均为 0。
- 项目管理：`project-to-act --validate` 返回 `valid=true, mode=managed`；未修改 `.project-to-act`。
- 反证 A：临时强制 `maximum_parallel=1` 后，`test_execute_read_plan_runs_three_independent_tasks_together` 以 `peak 1 != 3` 失败；恢复后通过。该过程同时修复了测试异常路径未在 `finally` 递减计数、可能产生假峰值的问题。
- 反证 B：临时令关键值守卫无条件放行后，`test_workspace_composite_rejects_answer_that_changes_verified_amount` 因错误金额 `4811.00` 穿透而失败；恢复后通过并保留 `4181.00`。
- 未运行全量仓库测试；遵照本轮用户要求，仅运行统筹专项与直接相关业务回归。
- 交付：Draft PR [#12](https://github.com/a1024053774/yunpai-ecommerce-agent/pull/12)，head 为 `zouchuheiye:codex/workspace-composite-queries`，base 为 `a1024053774:main`，明确依赖仍未合并的 Draft PR #9。
- 远端回读时 PR #12 为 `OPEN/Draft/DIRTY`；`DIRTY` 继承自 PR #9 相对当前 main 的冲突状态，因此不申请合并。等待 #9 同步或合并后，再同步最新 main、复验并请求复审。

---

## 完成门禁

只有同时满足以下条件，才能声明复合只读查询候选完成：

1. 库存+收入人工报告场景已由自动化测试复现并修复。
2. 三并发与依赖串行均有非时间猜测式测试证据。
3. 每个子目标均有结果状态，失败不会伪装为零。
4. 核实数字被回答模型改写时会切换确定性摘要。
5. 明确写请求继续返回确认卡，未进入只读线程池。
6. 统筹 Agent 专项、相关业务回归、编译和差异检查有新鲜通过证据。
7. 分支、PR 依赖和未完成边界描述准确；没有把 Draft 写成已合并或生产可用。
