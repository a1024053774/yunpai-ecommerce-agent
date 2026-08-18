from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business.inventory import InventoryService
from ecommerce_agent.business.orders import OrderService
from ecommerce_agent.database import Database
from ecommerce_agent.forecasting.planning import InventoryPlanningService
from ecommerce_agent.forecasting.product import ForecastProductService
from ecommerce_agent.forecasting.run_service import ForecastRunService
from ecommerce_agent.forecasting.service import DemandFactService

from conftest import make_settings


TENANT = "tenant-test"
STORE = "store-test"
ADMIN = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
DEMO_STORE = "virtual-shop-001"
DEMO_SKU = "YP-SKU-001"
POLICY_UPDATE = {
    "store_id": DEMO_STORE,
    "forecast_policy": {
        "policy_version": "forecast-wp4-v1",
        "horizons": [7, 14, 30],
        "minimum_history_days": 14,
        "backtest_windows": 4,
        "required_relative_improvement": 0.02,
        "interval_levels": [0.5, 0.8, 0.95],
        "candidate_models": [
            "last_value",
            "seasonal_naive_7",
            "rolling_mean",
            "weighted_moving_average",
            "ewma",
            "croston",
            "tsb",
        ],
    },
    "inventory_policy": {
        "supplier_lead_days": 7,
        "review_period_days": 7,
        "service_level": "0.80",
        "minimum_order_qty": "6",
        "order_multiple": "6",
        "minimum_safety_stock": "3",
        "maximum_stock_days": 30,
        "policy_version": "inventory-wp4-v1",
    },
}


def _make_db(path) -> Database:
    db = Database(path)
    db.initialize()
    return db


def _product(db) -> ForecastProductService:
    inventory = InventoryService(db)
    facts = DemandFactService(db, orders=OrderService(db), inventory=inventory)
    runs = ForecastRunService(db, facts=facts)
    plans = InventoryPlanningService(db, forecasts=runs, inventory=inventory)
    return ForecastProductService(db, facts=facts, runs=runs, plans=plans)


def _prepare_virtual_inputs(service) -> None:
    for resource in ("orders", "inventory"):
        result = service.operations.sync(
            tenant_id="tenant-test",
            connector_id="virtual_taobao",
            resource=resource,
            actor="admin-test",
        )
        assert result["virtual"] is True


def test_review_on_empty_store_returns_readonly_view(tmp_path) -> None:
    db = _make_db(tmp_path / "review.sqlite3")
    review = _product(db).review(
        tenant_id=TENANT, store_id=STORE, sku_id="sku-1"
    )
    assert review["sku_id"] == "sku-1"
    assert review["forecast"] is None
    assert review["backtest"] is None
    assert review["plan"] is None
    assert review["risks"] == []
    assert len(review["readiness"]) == 11
    assert all(
        item["signal_usage"] == "not_used"
        for item in review["readiness"]
        if item["category"] == "candidate_signal"
    )
    assert all(
        item["signal_usage"] is None
        for item in review["readiness"]
        if item["category"] != "candidate_signal"
    )


def test_run_batch_isolates_failures(tmp_path) -> None:
    db = _make_db(tmp_path / "batch.sqlite3")
    results = _product(db).run_batch(
        TENANT, store_id=STORE, sku_ids=["no-data-1", "no-data-2"]
    )
    assert len(results) == 2
    assert all(item["status"] == "failed" for item in results)
    assert all(
        "forecast_history_not_found" in item["error"] for item in results
    )


def test_rerun_returns_single_result(tmp_path) -> None:
    db = _make_db(tmp_path / "rerun.sqlite3")
    result = _product(db).rerun(TENANT, store_id=STORE, sku_id="no-data")
    assert result["sku_id"] == "no-data"
    assert result["status"] == "failed"


def test_review_endpoint_returns_readonly_view(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get(
            f"/v1/forecasting/skus/sku-1/review?store_id={STORE}",
            headers=ADMIN,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["sku_id"] == "sku-1"
    assert body["forecast"] is None
    assert body["plan"] is None


def test_batch_endpoint_isolates_failures(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/forecasting/batch/runs",
            json={"store_id": STORE, "sku_ids": ["no-data-1", "no-data-2"]},
            headers=ADMIN,
        )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert all(item["status"] == "failed" for item in results)


def test_batch_endpoint_runs_forecast_and_plan_for_seeded_sku(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    _prepare_virtual_inputs(app.state.agent)
    with TestClient(app) as client:
        rebuilt = client.post(
            "/v1/forecasting/demand/rebuild",
            headers=ADMIN,
            json={
                "store_id": DEMO_STORE,
                "sku_id": DEMO_SKU,
                "mode": "full",
                "start_date": "2026-06-01",
                "end_date": "2026-07-21",
                "coverage_complete": True,
            },
        )
        assert rebuilt.status_code == 200, rebuilt.text

        configured = client.put(
            f"/v1/forecasting/policies/{DEMO_SKU}",
            headers=ADMIN,
            json=POLICY_UPDATE,
        )
        assert configured.status_code == 200, configured.text

        response = client.post(
            "/v1/forecasting/batch/runs",
            headers=ADMIN,
            json={"store_id": DEMO_STORE, "sku_ids": [DEMO_SKU]},
        )
        assert response.status_code == 200, response.text
        first = response.json()["results"][0]
        assert first["status"] == "completed"
        assert first["forecast_run_id"]
        assert first["plan_id"]

        review = client.get(
            f"/v1/forecasting/skus/{DEMO_SKU}/review?store_id={DEMO_STORE}",
            headers=ADMIN,
        ).json()
        assert review["forecast"]["run_id"] == first["forecast_run_id"]
        assert review["plan"]["plan_id"] == first["plan_id"]
        assert len(review["forecast"]["points"]) == 30
        assert review["forecast"]["champion_model"]
        assert review["plan"]["action_mode"] == "advisory_only"
        assert review["signal_usage"] == "not_used"
        assert (
            review["forecast"]["candidate_models"]["signal_champion_reason"][
                "signal_usage"
            ]
            == "not_used"
        )
        first_recommended = review["plan"]["recommended_order_qty"]

        second_response = client.post(
            "/v1/forecasting/batch/runs",
            headers=ADMIN,
            json={"store_id": DEMO_STORE, "sku_ids": [DEMO_SKU]},
        )
        second = second_response.json()["results"][0]
        assert second["status"] == "completed"
        assert second["forecast_run_id"] != first["forecast_run_id"]

        second_review = client.get(
            f"/v1/forecasting/skus/{DEMO_SKU}/review?store_id={DEMO_STORE}",
            headers=ADMIN,
        ).json()
        assert second_review["forecast"]["run_id"] == second["forecast_run_id"]
        assert (
            second_review["plan"]["recommended_order_qty"]
            == first_recommended
        )
