from __future__ import annotations

import threading

import pytest

from ecommerce_agent.workspace_read_plan import (
    WorkspaceReadPlan,
    WorkspaceReadTask,
    WorkspaceTaskResult,
    execute_read_plan,
    ready_task_batches,
    validate_read_plan,
)


READ_TOOLS = {"get_inventory_risk", "get_business_metric"}


def _task(
    task_id: str,
    *,
    tool_name: str = "get_inventory_risk",
    depends_on: list[str] | None = None,
) -> WorkspaceReadTask:
    return WorkspaceReadTask(
        task_id=task_id,
        objective=f"Verify {task_id}",
        tool_name=tool_name,
        depends_on=depends_on or [],
    )


def test_read_plan_rejects_non_read_tool() -> None:
    plan = WorkspaceReadPlan(tasks=[_task("inventory", tool_name="write_inventory")])

    with pytest.raises(ValueError, match="read_tool_not_allowed"):
        validate_read_plan(plan, readable_tools=READ_TOOLS)


def test_read_plan_rejects_more_than_four_tasks() -> None:
    plan = WorkspaceReadPlan(tasks=[_task(str(index)) for index in range(5)])

    with pytest.raises(ValueError, match="read_plan_too_large"):
        validate_read_plan(plan, readable_tools=READ_TOOLS)


def test_read_plan_rejects_duplicate_task_ids() -> None:
    plan = WorkspaceReadPlan(tasks=[_task("inventory"), _task("inventory")])

    with pytest.raises(ValueError, match="read_task_id_duplicate"):
        validate_read_plan(plan, readable_tools=READ_TOOLS)


@pytest.mark.parametrize(
    ("dependency", "error_code"),
    [("missing", "read_dependency_unknown"), ("inventory", "read_dependency_self")],
)
def test_read_plan_rejects_invalid_dependency(
    dependency: str, error_code: str
) -> None:
    plan = WorkspaceReadPlan(
        tasks=[_task("inventory", depends_on=[dependency])]
    )

    with pytest.raises(ValueError, match=error_code):
        validate_read_plan(plan, readable_tools=READ_TOOLS)


def test_read_plan_rejects_dependency_cycle() -> None:
    plan = WorkspaceReadPlan(
        tasks=[
            _task("inventory", depends_on=["revenue"]),
            _task(
                "revenue",
                tool_name="get_business_metric",
                depends_on=["inventory"],
            ),
        ]
    )

    with pytest.raises(ValueError, match="read_plan_cycle"):
        validate_read_plan(plan, readable_tools=READ_TOOLS)


def test_read_plan_rejects_empty_plan_without_direct_response() -> None:
    with pytest.raises(ValueError, match="read_plan_empty"):
        validate_read_plan(WorkspaceReadPlan(), readable_tools=READ_TOOLS)


def test_read_plan_accepts_direct_response_without_tasks() -> None:
    plan = WorkspaceReadPlan(response="No live lookup is needed.")

    assert validate_read_plan(plan, readable_tools=READ_TOOLS) is plan


def test_ready_task_batches_caps_independent_queries_at_three() -> None:
    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abcd"])

    assert ready_task_batches(plan, batch_size=3) == [["a", "b", "c"], ["d"]]


def test_ready_task_batches_waits_for_dependencies() -> None:
    plan = WorkspaceReadPlan(
        tasks=[
            _task("search"),
            _task("competitor", depends_on=["search"]),
        ]
    )

    assert ready_task_batches(plan, batch_size=3) == [["search"], ["competitor"]]


def _success(task: WorkspaceReadTask) -> WorkspaceTaskResult:
    return WorkspaceTaskResult(
        task_id=task.task_id,
        objective=task.objective,
        tool_name=task.tool_name,
        tool_label="Business information",
        status="success",
        verified_facts=[f"Verified {task.task_id}"],
    )


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
        return _success(task)

    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abc"])
    results = execute_read_plan(plan, runner=runner, maximum_parallel=3)

    assert peak == 3
    assert [item.task_id for item in results] == ["a", "b", "c"]


def test_execute_read_plan_caps_four_tasks_at_three_and_preserves_order() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    first_batch = threading.Barrier(3)

    def runner(task: WorkspaceReadTask) -> WorkspaceTaskResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if task.task_id in {"a", "b", "c"}:
            first_batch.wait(timeout=2)
        with lock:
            active -= 1
        return _success(task)

    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abcd"])
    results = execute_read_plan(plan, runner=runner, maximum_parallel=3)

    assert peak == 3
    assert [item.task_id for item in results] == ["a", "b", "c", "d"]


def test_execute_read_plan_isolates_failure_and_skips_dependent_task() -> None:
    called: list[str] = []

    def runner(task: WorkspaceReadTask) -> WorkspaceTaskResult:
        called.append(task.task_id)
        if task.task_id == "inventory":
            raise ValueError("inventory_source_unavailable")
        return _success(task)

    plan = WorkspaceReadPlan(
        tasks=[
            _task("inventory"),
            _task("revenue", tool_name="get_business_metric"),
            _task("restock", depends_on=["inventory"]),
        ]
    )
    results = execute_read_plan(plan, runner=runner, maximum_parallel=3)

    assert set(called) == {"inventory", "revenue"}
    assert [item.status for item in results] == ["failed", "success", "skipped"]
    assert results[0].error_summary == "inventory_source_unavailable"
    assert results[2].error_summary == "Prerequisite information was not verified."


def test_execute_read_plan_skips_dependency_after_no_data() -> None:
    called: list[str] = []

    def runner(task: WorkspaceReadTask) -> WorkspaceTaskResult:
        called.append(task.task_id)
        return WorkspaceTaskResult(
            task_id=task.task_id,
            objective=task.objective,
            tool_name=task.tool_name,
            tool_label="Business information",
            status="no_data",
        )

    plan = WorkspaceReadPlan(
        tasks=[_task("search"), _task("competitor", depends_on=["search"])]
    )
    results = execute_read_plan(plan, runner=runner)

    assert called == ["search"]
    assert [item.status for item in results] == ["no_data", "skipped"]
