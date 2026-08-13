from __future__ import annotations

import pytest

from ecommerce_agent.workspace_read_plan import (
    WorkspaceReadPlan,
    WorkspaceReadTask,
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
