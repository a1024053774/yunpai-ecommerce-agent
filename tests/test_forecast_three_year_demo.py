from __future__ import annotations

from datetime import date

from ecommerce_agent.service import AgentService

from conftest import make_settings
from evals.forecasting.run_three_year_demo import demo_settings, run_three_year_demo


def test_three_year_demo_uses_complete_local_settings(tmp_path) -> None:
    settings = demo_settings(tmp_path / "demo-data")

    assert settings.data_dir == (tmp_path / "demo-data").resolve()
    assert settings.model_enabled is False
    assert settings.outbox_worker_enabled is False
    assert settings.channel_agent_worker_enabled is False


def test_three_year_virtual_demo_persists_ninety_day_actual_comparison(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        result = run_three_year_demo(service)

        assert result["virtual"] is True
        assert result["production_claim"] is False
        assert result["sales_day_count"] == 1095
        assert result["comparison_day_count"] == 90
        assert result["tenant_id"] == "local-appliance"
        assert result["training_start"] == date(2023, 8, 9).isoformat()
        assert result["training_end"] == date(2026, 8, 7).isoformat()
        assert result["run_id"]
        assert result["actual_vs_forecast"]
        assert len(result["actual_vs_forecast"]) == 90
        assert all(item["actual"] is not None and item["forecast"] is not None for item in result["actual_vs_forecast"])

        run = service.operations.forecasting.get_run("local-appliance", result["run_id"])
        champion_records = [
            item for item in run["backtests"] if item["model_name"] == run["champion_model"]
        ]
        assert len(champion_records) == 3
        assert sum(len(item["actual"]) for item in champion_records) == 90
        assert len(run["forecast_points"]) == 30
        assert run["data_quality"]["quality_level"] == "good"
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0] == 1095
            assert conn.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM forecast_backtests").fetchone()[0] >= 3
            assert conn.execute("SELECT COUNT(*) FROM forecast_points").fetchone()[0] == 30
    finally:
        service.close()
