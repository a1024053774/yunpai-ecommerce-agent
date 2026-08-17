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
