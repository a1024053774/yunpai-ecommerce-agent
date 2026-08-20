from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
import json
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceReadTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    objective: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class WorkspaceReadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: str | None = Field(default=None, max_length=2400)
    tasks: list[WorkspaceReadTask] = Field(default_factory=list)


class WorkspaceTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    objective: str
    tool_name: str
    tool_label: str
    status: Literal["success", "no_data", "failed", "skipped"]
    verified_facts: list[str] = Field(default_factory=list)
    critical_values: list[str] = Field(default_factory=list)
    error_summary: str | None = None
    data_as_of: str | None = None


ReadTaskRunner = Callable[
    [WorkspaceReadTask, dict[str, "WorkspaceTaskResult"]], WorkspaceTaskResult
]


def validate_read_plan(
    plan: WorkspaceReadPlan,
    *,
    readable_tools: set[str],
    maximum_tasks: int = 4,
) -> WorkspaceReadPlan:
    if not plan.tasks:
        if plan.response and plan.response.strip():
            return plan
        raise ValueError("read_plan_empty")
    if len(plan.tasks) > maximum_tasks:
        raise ValueError("read_plan_too_large")

    task_ids = [task.task_id for task in plan.tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("read_task_id_duplicate")

    known_ids = set(task_ids)
    for task in plan.tasks:
        if task.tool_name not in readable_tools:
            raise ValueError("read_tool_not_allowed")
        for dependency in task.depends_on:
            if dependency == task.task_id:
                raise ValueError("read_dependency_self")
            if dependency not in known_ids:
                raise ValueError("read_dependency_unknown")

    dependencies = {task.task_id: task.depends_on for task in plan.tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("read_plan_cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_ids:
        visit(task_id)
    return plan


def ready_task_batches(
    plan: WorkspaceReadPlan, *, batch_size: int = 3
) -> list[list[str]]:
    if batch_size < 1:
        raise ValueError("read_batch_size_invalid")

    remaining = list(plan.tasks)
    completed: set[str] = set()
    batches: list[list[str]] = []
    while remaining:
        ready = [
            task
            for task in remaining
            if set(task.depends_on).issubset(completed)
        ]
        if not ready:
            raise ValueError("read_plan_cycle")
        batch = ready[:batch_size]
        batch_ids = [task.task_id for task in batch]
        batches.append(batch_ids)
        completed.update(batch_ids)
        remaining = [task for task in remaining if task.task_id not in completed]
    return batches


def execute_read_plan(
    plan: WorkspaceReadPlan,
    *,
    runner: ReadTaskRunner,
    maximum_parallel: int = 3,
    task_timeout_seconds: float | None = None,
    plan_timeout_seconds: float | None = None,
) -> list[WorkspaceTaskResult]:
    if maximum_parallel < 1:
        raise ValueError("read_parallelism_invalid")
    if task_timeout_seconds is not None and task_timeout_seconds <= 0:
        raise ValueError("read_task_timeout_invalid")
    if plan_timeout_seconds is not None and plan_timeout_seconds <= 0:
        raise ValueError("read_plan_timeout_invalid")

    tasks_by_id = {task.task_id: task for task in plan.tasks}
    results_by_id: dict[str, WorkspaceTaskResult] = {}
    results_by_signature: dict[str, WorkspaceTaskResult] = {}
    deadline = (
        monotonic() + plan_timeout_seconds
        if plan_timeout_seconds is not None
        else None
    )

    def failed_result(task: WorkspaceReadTask, error_summary: str) -> WorkspaceTaskResult:
        return WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="Business information",
            status="failed",
            error_summary=error_summary,
        )

    for batch in ready_task_batches(plan, batch_size=maximum_parallel):
        runnable: list[WorkspaceReadTask] = []
        aliases: dict[str, str] = {}
        pending_signatures: dict[str, str] = {}
        for task_id in batch:
            task = tasks_by_id[task_id]
            dependency_results = [
                results_by_id[item] for item in task.depends_on
            ]
            if any(item.status != "success" for item in dependency_results):
                results_by_id[task_id] = WorkspaceTaskResult(
                    task_id=task.task_id,
                    objective=task.objective,
                    tool_name=task.tool_name,
                    tool_label="Business information",
                    status="skipped",
                    error_summary="Prerequisite information was not verified.",
                )
                continue
            signature = json.dumps(
                {"tool_name": task.tool_name, "arguments": task.arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            existing = results_by_signature.get(signature)
            if existing is not None:
                results_by_id[task_id] = existing.model_copy(
                    update={"task_id": task.task_id, "objective": task.objective}
                )
                continue
            if signature in pending_signatures:
                aliases[task_id] = pending_signatures[signature]
                continue
            pending_signatures[signature] = task_id
            runnable.append(task)

        if not runnable:
            continue
        pool = ThreadPoolExecutor(max_workers=min(maximum_parallel, len(runnable)))
        try:
            futures = [
                (task, pool.submit(
                    runner,
                    task,
                    {dep: results_by_id[dep] for dep in task.depends_on},
                ))
                for task in runnable
            ]
            timeout = task_timeout_seconds
            plan_timeout_wins = False
            if deadline is not None:
                remaining = max(0.0, deadline - monotonic())
                if timeout is None or remaining <= timeout:
                    timeout = remaining
                    plan_timeout_wins = True
            completed, pending = wait(
                [future for _, future in futures],
                timeout=timeout,
            )
            for future in pending:
                future.cancel()
            for task, future in futures:
                if future not in completed:
                    result = failed_result(
                        task,
                        "read_plan_timeout"
                        if plan_timeout_wins
                        else "read_timeout",
                    )
                else:
                    try:
                        result = future.result()
                    except Exception as exc:  # Each read failure is isolated to its task.
                        result = failed_result(task, str(exc))
                results_by_id[task.task_id] = result
                signature = json.dumps(
                    {"tool_name": task.tool_name, "arguments": task.arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                results_by_signature[signature] = result
        finally:
            # 不等待仍在跑的线程：超时后立即收口，保证墙钟时间受 task/plan 超时约束。
            pool.shutdown(wait=False, cancel_futures=True)

        for alias_id, source_id in aliases.items():
            source = results_by_id[source_id]
            alias = tasks_by_id[alias_id]
            results_by_id[alias_id] = source.model_copy(
                update={"task_id": alias.task_id, "objective": alias.objective}
            )

    return [results_by_id[task.task_id] for task in plan.tasks]
