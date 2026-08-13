from __future__ import annotations

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
