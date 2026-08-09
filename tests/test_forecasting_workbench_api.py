from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import DemandFactService, InventoryBalanceUpsert, OrderUpsert
from ecommerce_agent.business.forecasting import ForecastRunRequest
from ecommerce_agent.business.inventory_planning import InventoryPlanningPolicy
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
TENANT = "tenant-test"
STORE = "forecast-api-store"
SKU = "forecast-api-sku"
WAREHOUSE = "forecast-api-warehouse"
ADMIN_HEADERS = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}


def _seed_orders(service, *, days: int = 35) -> None:
    for index in range(days):
        business_date = date(2026, 7, 1) + timedelta(days=index)
        placed_at = datetime.combine(business_date, time(12), tzinfo=SHANGHAI).astimezone(UTC)
        service.operations.orders.upsert(
            TENANT,
            OrderUpsert(
                connector_id="forecast-api-connector",
                store_id=STORE,
                order_id=f"forecast-api-order-{index}",
                order_status="paid",
                payment_status="paid",
                currency="CNY",
                total_amount=Decimal("20"),
                placed_at=placed_at,
                lines=[
                    OrderLineInput(
                        line_id=f"forecast-api-line-{index}",
                        sku_id=SKU,
                        title="Forecast API fixture",
                        quantity=(index % 7) + 1,
                        unit_price=Decimal("20"),
                    )
                ],
                source_updated_at=placed_at,
                source_id=f"forecast-api-source-{index}",
            ),
        )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=TENANT,
        client_id="client-test",
        session_id="forecast-tool-session",
        trace_id="forecast-tool-trace",
        trusted_context={},
    )


def test_forecasting_api_exposes_replayable_demand_run_plan_and_risks(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        demand_facts = DemandFactService(
            service.db,
            orders=service.operations.orders,
            inventory=service.operations.inventory,
            now_provider=lambda: datetime(2026, 8, 12, 1, tzinfo=SHANGHAI).astimezone(UTC),
        )
        service.operations.demand_facts = demand_facts
        service.operations.forecasting.demand_facts = demand_facts
        _seed_orders(service, days=42)
        service.operations.inventory.upsert(
            TENANT,
            InventoryBalanceUpsert(
                connector_id="forecast-api-inventory",
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                on_hand=Decimal("30"),
                reserved=Decimal("5"),
                inbound=Decimal("5"),
                source_updated_at=datetime.now(UTC),
                source_id="forecast-api-inventory-source",
            ),
        )
        with TestClient(app) as client:
            assert client.get(
                f"/v1/forecasting/demand?store_id={STORE}&sku_id={SKU}"
            ).status_code in (401, 503)
            rebuilt = client.post(
                "/v1/forecasting/demand/rebuild",
                headers=ADMIN_HEADERS,
                json={
                    "store_id": STORE,
                    "sku_id": SKU,
                    "start_date": "2026-07-01",
                    "end_date": "2026-08-04",
                },
            )
            assert rebuilt.status_code == 200
            assert len(rebuilt.json()["facts"]) == 35

            demand = client.get(
                f"/v1/forecasting/demand?store_id={STORE}&sku_id={SKU}",
                headers=ADMIN_HEADERS,
            )
            assert demand.status_code == 200
            assert demand.json()["policy_version"] == "demand-v1"
            assert demand.json()["timezone"] == "Asia/Shanghai"
            assert demand.json()["basis"] == {
                "event_time": "placed_at",
                "label": "已支付订单的下单日销量",
            }
            assert demand.json()["training_closed_through"]
            assert demand.json()["today_so_far"]["basis_label"] == "已支付订单的下单日销量"

            policy = client.put(
                f"/v1/forecasting/policies/{SKU}",
                headers=ADMIN_HEADERS,
                json={"store_id": STORE, "sku_id": SKU, "horizon_days": 7},
            )
            assert policy.status_code == 200
            assert policy.json()["policy_version"] == 1

            run = client.post(
                "/v1/forecasting/runs",
                headers=ADMIN_HEADERS,
                json={"store_id": STORE, "sku_id": SKU, "horizon_days": 7},
            )
            assert run.status_code == 200
            run_body = run.json()
            assert run_body["forecast_policy_version"] == "2"
            assert len(run_body["forecast_points"]) == 7
            shadow = client.get(
                f"/v1/forecasting/runs/{run_body['run_id']}/shadow",
                headers=ADMIN_HEADERS,
            )
            assert shadow.status_code == 200
            assert shadow.json()["status"] == "pending"
            assert shadow.json()["persisted"] is False
            extended = client.post(
                "/v1/forecasting/demand/rebuild",
                headers=ADMIN_HEADERS,
                json={
                    "store_id": STORE,
                    "sku_id": SKU,
                    "start_date": "2026-07-01",
                    "end_date": "2026-08-11",
                },
            )
            assert extended.status_code == 200, extended.text
            evaluated_shadow = client.get(
                f"/v1/forecasting/runs/{run_body['run_id']}/shadow",
                headers=ADMIN_HEADERS,
            )
            assert evaluated_shadow.status_code == 200
            assert evaluated_shadow.json()["status"] == "evaluated"
            assert evaluated_shadow.json()["persisted"] is False
            assert set(evaluated_shadow.json()["pinball_loss"]) == {"p50", "p80", "p95"}
            assert set(evaluated_shadow.json()["interval_coverage"]) == {"p80", "p95"}

            latest = client.get(
                f"/v1/forecasting/skus/{SKU}/forecast?store_id={STORE}",
                headers=ADMIN_HEADERS,
            )
            assert latest.status_code == 200
            assert latest.json()["run_id"] == run_body["run_id"]
            backtest = client.get(
                f"/v1/forecasting/skus/{SKU}/backtest?store_id={STORE}",
                headers=ADMIN_HEADERS,
            )
            assert backtest.status_code == 200
            assert backtest.json()["run_id"] == run_body["run_id"]
            assert backtest.json()["backtest_summary"]
            assert backtest.json()["backtests"]

            planning_policy = client.put(
                f"/v1/forecasting/planning-policies/{SKU}",
                headers=ADMIN_HEADERS,
                json={
                    "store_id": STORE,
                    "sku_id": SKU,
                    "warehouse_id": WAREHOUSE,
                    "supplier_lead_days": 3,
                    "review_period_days": 2,
                    "service_level": "p80",
                    "minimum_order_qty": "10",
                    "order_multiple": "5",
                    "minimum_safety_stock": "3",
                },
            )
            assert planning_policy.status_code == 200

            plan = client.post(
                f"/v1/forecasting/skus/{SKU}/inventory-plan?store_id={STORE}",
                headers=ADMIN_HEADERS,
                json={"warehouse_id": WAREHOUSE, "forecast_run_id": run_body["run_id"]},
            )
            assert plan.status_code == 200
            assert plan.json()["status"] == "draft"
            assert plan.json()["external_order_created"] is False
            assert plan.json()["replenishment_order"]["kind"] == "forecast_replenishment"
            assert plan.json()["explanation"]["warehouse_scope"] == "supply_location_only"
            assert plan.json()["recommended_order_qty"]

            latest_plan = client.get(
                f"/v1/forecasting/skus/{SKU}/inventory-plan?store_id={STORE}&warehouse_id={WAREHOUSE}",
                headers=ADMIN_HEADERS,
            )
            assert latest_plan.status_code == 200
            risks = client.get("/v1/forecasting/risks", headers=ADMIN_HEADERS)
            assert risks.status_code == 200
            assert risks.json()["risks"]
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0] == 42
            assert conn.execute("SELECT COUNT(*) FROM inventory_plans").fetchone()[0] == 1
    finally:
        service.close()


def test_forecasting_tools_are_read_only_and_tenant_scoped(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    service = app.state.agent
    try:
        _seed_orders(service)
        service.operations.demand_facts.rebuild(
            TENANT,
            store_id=STORE,
            sku_id=SKU,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 8, 4),
        )
        run = service.operations.forecasting.run(
            TENANT,
            ForecastRunRequest(store_id=STORE, sku_id=SKU),
        )
        service.operations.inventory.upsert(
            TENANT,
            InventoryBalanceUpsert(
                connector_id="forecast-tool-inventory",
                store_id=STORE,
                warehouse_id=WAREHOUSE,
                sku_id=SKU,
                on_hand=Decimal("10"),
                reserved=Decimal("0"),
                inbound=Decimal("0"),
                source_updated_at=datetime.now(UTC),
                source_id="forecast-tool-inventory-source",
            ),
        )
        service.operations.inventory_planning.upsert_policy(
            TENANT,
            InventoryPlanningPolicy(
                store_id=STORE, sku_id=SKU, warehouse_id=WAREHOUSE,
                supplier_lead_days=2, review_period_days=2,
            ),
        )
        service.operations.inventory_planning.create_plan(
            TENANT, forecast_run_id=run["run_id"], warehouse_id=WAREHOUSE
        )
        context = _context()
        before = len(service.operations.inventory_planning.list_plans(TENANT))
        for name, arguments in (
            ("get_demand_forecast", {"store_id": STORE, "sku_id": SKU}),
            ("get_inventory_plan", {"store_id": STORE, "sku_id": SKU, "warehouse_id": WAREHOUSE}),
        ):
            spec, validated = service.tools.validate_selection(
                name=name, arguments=arguments, requested_mode="observe", context=context
            )
            assert spec.kind == "read"
            result = service.tools.execute(spec=spec, arguments=validated, context=context)
            assert result.status == "success"
            assert result.output["evidence"]["data_quality"]
            assert result.output["evidence"]["forecast_run_id"]
        scoped_context = ToolExecutionContext(
            tenant_id=TENANT,
            client_id="client-test",
            session_id="forecast-tool-session",
            trace_id="forecast-tool-trace",
            trusted_context={"store_id": "another-store"},
        )
        with pytest.raises(ValueError, match="tool_policy_denied:store_scope_mismatch"):
            service.tools.validate_selection(
                name="get_demand_forecast",
                arguments={"store_id": STORE, "sku_id": SKU},
                requested_mode="observe",
                context=scoped_context,
            )
        assert len(service.operations.inventory_planning.list_plans(TENANT)) == before
        missing = service.operations.forecasting.latest_run(
            "tenant-other", store_id=STORE, sku_id=SKU
        )
        assert missing is None
    finally:
        service.close()
