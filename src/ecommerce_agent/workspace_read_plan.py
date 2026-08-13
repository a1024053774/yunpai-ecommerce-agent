from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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


ReadTaskRunner = Callable[[WorkspaceReadTask], WorkspaceTaskResult]


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
) -> list[WorkspaceTaskResult]:
    if maximum_parallel < 1:
        raise ValueError("read_parallelism_invalid")

    tasks_by_id = {task.task_id: task for task in plan.tasks}
    results_by_id: dict[str, WorkspaceTaskResult] = {}
    for batch in ready_task_batches(plan, batch_size=maximum_parallel):
        runnable: list[WorkspaceReadTask] = []
        for task_id in batch:
            task = tasks_by_id[task_id]
            dependency_results = [results_by_id[item] for item in task.depends_on]
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
            runnable.append(task)

        if not runnable:
            continue
        with ThreadPoolExecutor(max_workers=min(maximum_parallel, len(runnable))) as pool:
            futures = {pool.submit(runner, task): task for task in runnable}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    results_by_id[task.task_id] = future.result()
                except Exception as exc:  # Each read failure is isolated to its task.
                    results_by_id[task.task_id] = WorkspaceTaskResult(
                        task_id=task.task_id,
                        objective=task.objective,
                        tool_name=task.tool_name,
                        tool_label="Business information",
                        status="failed",
                        error_summary=str(exc),
                    )

    return [results_by_id[task.task_id] for task in plan.tasks]
