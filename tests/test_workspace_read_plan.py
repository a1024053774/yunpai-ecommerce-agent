from __future__ import annotations

import threading
import time

import pytest

from ecommerce_agent.workspace_read_plan import (
    WorkspaceReadPlan,
    WorkspaceReadTask,
    WorkspaceTaskResult,
    execute_read_plan,
    ready_task_batches,
    validate_read_plan,
)


READ_TOOLS = {
    "get_inventory_risk",
    "get_business_metric",
    "get_catalog_status",
}


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
        arguments={"test_scope": task_id},
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


def test_read_plan_rejects_argument_reference_outside_declared_dependencies() -> None:
    plan = WorkspaceReadPlan(
        tasks=[
            _task("catalog", tool_name="get_catalog_status"),
            WorkspaceReadTask(
                task_id="inventory",
                objective="Verify selected inventory",
                tool_name="get_inventory_risk",
                arguments={"store_id": "store-001"},
                argument_refs={
                    "sku_id": {
                        "task_id": "catalog",
                        "path": ["items", 0, "sku_id"],
                    }
                },
                depends_on=[],
            ),
        ]
    )

    with pytest.raises(
        ValueError, match="read_dependency_reference_not_declared"
    ):
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
            _task(
                "competitor",
                tool_name="get_business_metric",
                depends_on=["search"],
            ),
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


def test_workspace_task_result_keeps_structured_data_internal() -> None:
    result = WorkspaceTaskResult(
        task_id="catalog",
        objective="Verify catalog",
        tool_name="get_catalog_status",
        tool_label="Business information",
        status="success",
        structured_data={"items": [{"sku_id": "SKU-001"}]},
    )

    assert "structured_data" not in result.model_dump()


def test_execute_read_plan_runs_three_independent_tasks_together() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=2)
        finally:
            with lock:
                active -= 1
        return _success(task)

    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abc"])
    results = execute_read_plan(plan, runner=runner, maximum_parallel=3)

    assert peak == 3
    assert [item.task_id for item in results] == ["a", "b", "c"]
    assert all(item.status == "success" for item in results)


def test_execute_read_plan_caps_four_tasks_at_three_and_preserves_order() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()
    first_batch = threading.Barrier(3)

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if task.task_id in {"a", "b", "c"}:
                first_batch.wait(timeout=2)
        finally:
            with lock:
                active -= 1
        return _success(task)

    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abcd"])
    results = execute_read_plan(plan, runner=runner, maximum_parallel=3)

    assert peak == 3
    assert [item.task_id for item in results] == ["a", "b", "c", "d"]
    assert all(item.status == "success" for item in results)


def test_execute_read_plan_isolates_failure_and_skips_dependent_task() -> None:
    called: list[str] = []

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
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

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
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


def test_execute_read_plan_reuses_identical_tool_arguments_for_multiple_objectives() -> None:
    calls: list[str] = []
    plan = WorkspaceReadPlan(
        tasks=[
            WorkspaceReadTask(
                task_id="inventory_summary",
                objective="Summarize inventory",
                tool_name="get_inventory_risk",
                arguments={"store_id": "store-001", "target_days": 30},
            ),
            WorkspaceReadTask(
                task_id="inventory_alerts",
                objective="List inventory alerts",
                tool_name="get_inventory_risk",
                arguments={"target_days": 30, "store_id": "store-001"},
            ),
        ]
    )

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
        calls.append(task.task_id)
        return _success(task)

    results = execute_read_plan(plan, runner=runner)

    assert calls == ["inventory_summary"]
    assert [item.task_id for item in results] == [
        "inventory_summary",
        "inventory_alerts",
    ]
    assert [item.objective for item in results] == [
        "Summarize inventory",
        "List inventory alerts",
    ]
    assert all(item.status == "success" for item in results)


def test_execute_read_plan_does_not_deduplicate_distinct_argument_references() -> None:
    calls: list[str] = []
    plan = WorkspaceReadPlan(
        tasks=[
            _task("catalog_a", tool_name="get_catalog_status"),
            _task("catalog_b", tool_name="get_business_metric"),
            WorkspaceReadTask(
                task_id="inventory_a",
                objective="Verify first selected item",
                tool_name="get_inventory_risk",
                arguments={"store_id": "store-001"},
                argument_refs={
                    "sku_id": {
                        "task_id": "catalog_a",
                        "path": ["items", 0, "sku_id"],
                    }
                },
                depends_on=["catalog_a"],
            ),
            WorkspaceReadTask(
                task_id="inventory_b",
                objective="Verify second selected item",
                tool_name="get_inventory_risk",
                arguments={"store_id": "store-001"},
                argument_refs={
                    "sku_id": {
                        "task_id": "catalog_b",
                        "path": ["items", 0, "sku_id"],
                    }
                },
                depends_on=["catalog_b"],
            ),
        ]
    )

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        calls.append(task.task_id)
        return _success(task)

    execute_read_plan(plan, runner=runner)

    assert "inventory_a" in calls
    assert "inventory_b" in calls


def test_execute_read_plan_completes_dependency_before_running_child() -> None:
    timeline: list[str] = []
    plan = WorkspaceReadPlan(
        tasks=[
            _task("search"),
            _task("competitor", depends_on=["search"]),
        ]
    )

    def runner(task: WorkspaceReadTask, predecessors: dict[str, WorkspaceTaskResult]) -> WorkspaceTaskResult:
        timeline.append(f"start:{task.task_id}")
        result = _success(task)
        timeline.append(f"finish:{task.task_id}")
        return result

    results = execute_read_plan(plan, runner=runner)

    assert timeline == [
        "start:search",
        "finish:search",
        "start:competitor",
        "finish:competitor",
    ]


def test_execute_read_plan_passes_predecessor_results_to_dependent_tasks() -> None:
    seen: dict[str, dict[str, WorkspaceTaskResult]] = {}
    plan = WorkspaceReadPlan(
        tasks=[_task("search"), _task("competitor", depends_on=["search"])]
    )

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        seen[task.task_id] = predecessors
        return _success(task)

    execute_read_plan(plan, runner=runner)
    assert set(seen["competitor"]) == {"search"}
    assert seen["competitor"]["search"].verified_facts == ["Verified search"]


def test_execute_read_plan_times_out_slow_tasks() -> None:
    plan = WorkspaceReadPlan(tasks=[_task("slow")])

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        time.sleep(1)
        return _success(task)

    results = execute_read_plan(
        plan, runner=runner, task_timeout_seconds=0.05
    )
    assert results[0].status == "failed"
    assert results[0].error_summary == "read_timeout"


def test_execute_read_plan_bounds_wall_clock_on_task_timeout() -> None:
    plan = WorkspaceReadPlan(tasks=[_task("slow")])

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        time.sleep(1)
        return _success(task)

    started = time.monotonic()
    results = execute_read_plan(plan, runner=runner, task_timeout_seconds=0.05)
    elapsed = time.monotonic() - started
    assert results[0].status == "failed"
    assert results[0].error_summary == "read_timeout"
    assert elapsed < 0.5


def test_execute_read_plan_shares_timeout_budget_across_parallel_tasks() -> None:
    plan = WorkspaceReadPlan(tasks=[_task(task_id) for task_id in "abc"])

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        time.sleep(0.5)
        return _success(task)

    started = time.monotonic()
    results = execute_read_plan(
        plan,
        runner=runner,
        maximum_parallel=3,
        task_timeout_seconds=0.1,
    )
    elapsed = time.monotonic() - started

    assert [item.status for item in results] == ["failed", "failed", "failed"]
    assert {item.error_summary for item in results} == {"read_timeout"}
    assert elapsed < 0.22


def test_execute_read_plan_plan_timeout_marks_plan_timeout_and_bounds_wall_clock() -> None:
    plan = WorkspaceReadPlan(tasks=[_task("a"), _task("b")])

    def runner(
        task: WorkspaceReadTask,
        predecessors: dict[str, WorkspaceTaskResult],
    ) -> WorkspaceTaskResult:
        time.sleep(0.6)
        return _success(task)

    started = time.monotonic()
    results = execute_read_plan(plan, runner=runner, plan_timeout_seconds=0.1)
    elapsed = time.monotonic() - started
    assert all(item.status == "failed" for item in results)
    assert {item.error_summary for item in results} == {"read_plan_timeout"}
    assert elapsed < 0.5
