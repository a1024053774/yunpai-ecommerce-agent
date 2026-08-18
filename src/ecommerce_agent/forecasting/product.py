"""M10-R WP2 — M6-R 预测与补货产品化编排层。

复用 Demand Fact / Forecast Engine / Inventory Planning，提供批量 SKU 运行、
单 SKU 失败隔离、显式重跑和只读计划审核。不另建预测算法，不绕过模型数值。
"""

from __future__ import annotations

from typing import Any

from .planning import InventoryPlanningError, InventoryPlanningPolicy
from .readiness import ReadinessCategory, SignalReadinessService
from .run_service import ForecastRunError


class ForecastProductError(ValueError):
    """产品化编排错误。"""


class ForecastProductService:
    def __init__(self, db: Any, *, facts: Any, runs: Any, plans: Any) -> None:
        self._db = db
        self._facts = facts
        self._runs = runs
        self._plans = plans
        self._readiness = SignalReadinessService(db)

    def run_batch(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_ids: list[str],
        forecast_policy: Any | None = None,
        planning_policy: InventoryPlanningPolicy | None = None,
    ) -> list[dict[str, Any]]:
        """逐 SKU 运行 forecast + plan，单 SKU 失败不影响其余。"""
        results: list[dict[str, Any]] = []
        for sku_id in sku_ids:
            try:
                run = self._runs.run(
                    tenant_id,
                    store_id=store_id,
                    sku_id=sku_id,
                    policy=forecast_policy,
                )
                plan_policy = planning_policy or self._plans.resolve_policy(
                    tenant_id, store_id=store_id, sku_id=sku_id
                )
                if plan_policy is None:
                    raise InventoryPlanningError("planning_policy_not_found")
                plan = self._plans.create_plan(
                    tenant_id, run["run_id"], plan_policy
                )
                results.append(
                    {
                        "sku_id": sku_id,
                        "status": "completed",
                        "forecast_run_id": run["run_id"],
                        "plan_id": plan["plan_id"],
                    }
                )
            except (ForecastRunError, InventoryPlanningError, KeyError) as exc:
                results.append(
                    {
                        "sku_id": sku_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        return results

    def rerun(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str,
        forecast_policy: Any | None = None,
        planning_policy: InventoryPlanningPolicy | None = None,
    ) -> dict[str, Any]:
        """对单个 SKU 显式重跑 forecast + plan（生成新 run）。"""
        results = self.run_batch(
            tenant_id,
            store_id=store_id,
            sku_ids=[sku_id],
            forecast_policy=forecast_policy,
            planning_policy=planning_policy,
        )
        return results[0]

    def review(
        self, *, tenant_id: str, store_id: str, sku_id: str
    ) -> dict[str, Any]:
        """只读审核：最新 forecast、backtest、plan、风险和准备度。"""
        try:
            forecast = self._runs.latest_run(
                tenant_id, sku_id=sku_id, store_id=store_id
            )
        except ValueError:
            forecast = None
        try:
            backtest = self._runs.latest_backtest(
                tenant_id, sku_id=sku_id, store_id=store_id
            )
        except ValueError:
            backtest = None
        try:
            plan = self._plans.latest_plan(
                tenant_id, sku_id=sku_id, store_id=store_id
            )
        except ValueError:
            plan = None
        risks = self._plans.list_risks(
            tenant_id, store_id=store_id, sku_id=sku_id
        )
        readiness = self._readiness.project(
            tenant_id=tenant_id, store_id=store_id
        )
        return {
            "sku_id": sku_id,
            "forecast": forecast,
            "backtest": backtest,
            "plan": plan,
            "risks": risks,
            "readiness": [
                {
                    "input_key": item.input_key,
                    "category": item.category.value,
                    "evidence_state": item.evidence_state.value,
                    "source_kind": item.source_kind.value if item.source_kind else None,
                    "data_as_of": item.data_as_of,
                    "sku_coverage": item.sku_coverage,
                    "missing_reason": item.missing_reason,
                    "signal_usage": (
                        "not_used"
                        if item.category is ReadinessCategory.CANDIDATE_SIGNAL
                        else None
                    ),
                }
                for item in readiness
            ],
        }
