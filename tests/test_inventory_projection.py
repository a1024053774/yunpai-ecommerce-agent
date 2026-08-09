from __future__ import annotations

from datetime import date
from decimal import Decimal

from ecommerce_agent.business.inventory_projection import (
    ForecastDemandPoint,
    InventoryProjectionService,
    ReplenishmentPlanner,
    ScheduledInbound,
)


def test_projection_adds_inbound_only_on_its_expected_arrival_day() -> None:
    """Moving inbound supply to day one would overstate stock before it arrives."""
    result = InventoryProjectionService().project(
        on_hand=Decimal("5"),
        reserved=Decimal("0"),
        forecast_points=[
            ForecastDemandPoint(date(2026, 8, 10), Decimal("4")),
            ForecastDemandPoint(date(2026, 8, 11), Decimal("4")),
            ForecastDemandPoint(date(2026, 8, 12), Decimal("4")),
        ],
        scheduled_inbounds=[
            ScheduledInbound(quantity=Decimal("10"), expected_arrival_date=date(2026, 8, 11)),
        ],
        return_rate=Decimal("0"),
    )

    assert result.starting_available == Decimal("5")
    assert result.days[0].opening_available == Decimal("5")
    assert result.days[0].scheduled_inbound == Decimal("0")
    assert result.days[0].closing_available == Decimal("1")
    assert result.days[1].opening_available == Decimal("1")
    assert result.days[1].scheduled_inbound == Decimal("10")
    assert result.days[1].closing_available == Decimal("7")
    assert result.first_stockout_date is None


def test_projection_reports_unknown_arrival_supply_without_using_it() -> None:
    """Counting undated inbound supply would hide a real near-term stockout."""
    result = InventoryProjectionService().project(
        on_hand=Decimal("2"),
        reserved=Decimal("0"),
        forecast_points=[ForecastDemandPoint(date(2026, 8, 10), Decimal("4"))],
        scheduled_inbounds=[ScheduledInbound(quantity=Decimal("8"), expected_arrival_date=None)],
        return_rate=Decimal("0"),
    )

    assert result.days[0].scheduled_inbound == Decimal("0")
    assert result.days[0].unmet_demand == Decimal("2")
    assert result.first_stockout_date == date(2026, 8, 10)
    assert result.unknown_arrival_inbound_quantity == Decimal("8")


def test_projection_uses_only_the_explicit_return_rate_adjustment() -> None:
    """Removing the configured return adjustment would overstate the stockout gap."""
    result = InventoryProjectionService().project(
        on_hand=Decimal("5"),
        reserved=Decimal("0"),
        forecast_points=[ForecastDemandPoint(date(2026, 8, 10), Decimal("10"))],
        return_rate=Decimal("0.2"),
    )

    assert result.days[0].return_adjustment == Decimal("2")
    assert result.days[0].closing_available == Decimal("0")
    assert result.days[0].unmet_demand == Decimal("3")


def test_replenishment_draft_applies_safety_stock_moq_then_order_multiple() -> None:
    """Applying the multiple before MOQ would produce a smaller, invalid order."""
    draft = ReplenishmentPlanner().draft(
        on_hand=Decimal("5"),
        reserved=Decimal("0"),
        forecast_points=[
            ForecastDemandPoint(date(2026, 8, 10), Decimal("4")),
            ForecastDemandPoint(date(2026, 8, 11), Decimal("4")),
            ForecastDemandPoint(date(2026, 8, 12), Decimal("4")),
        ],
        scheduled_inbounds=[
            ScheduledInbound(quantity=Decimal("10"), expected_arrival_date=date(2026, 8, 11)),
        ],
        return_rate=Decimal("0"),
        lead_time_days=1,
        review_period_days=2,
        safety_stock_days=1,
        minimum_safety_stock=Decimal("0"),
        minimum_order_qty=Decimal("5"),
        order_multiple=Decimal("3"),
    )

    assert draft.status == "draft"
    assert draft.persisted is False
    assert draft.external_order_created is False
    assert draft.target_demand == Decimal("12")
    assert draft.safety_stock == Decimal("4")
    assert draft.raw_quantity == Decimal("1")
    assert draft.after_minimum_order_qty == Decimal("5")
    assert draft.recommended_quantity == Decimal("6")
