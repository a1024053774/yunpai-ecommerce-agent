from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Sequence


@dataclass(frozen=True)
class ForecastDemandPoint:
    business_date: date
    forecast_sales: Decimal

    def __post_init__(self) -> None:
        if self.forecast_sales < 0:
            raise ValueError("inventory_projection_negative_forecast")


@dataclass(frozen=True)
class ScheduledInbound:
    quantity: Decimal
    expected_arrival_date: date | None

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("inventory_projection_negative_inbound")


@dataclass(frozen=True)
class InventoryProjectionDay:
    business_date: date
    opening_available: Decimal
    scheduled_inbound: Decimal
    return_adjustment: Decimal
    forecast_sales: Decimal
    closing_available: Decimal
    unmet_demand: Decimal


@dataclass(frozen=True)
class InventoryProjection:
    starting_available: Decimal
    days: tuple[InventoryProjectionDay, ...]
    first_stockout_date: date | None
    unknown_arrival_inbound_quantity: Decimal


@dataclass(frozen=True)
class ReplenishmentDraft:
    status: str
    persisted: bool
    external_order_created: bool
    projection: InventoryProjection
    lead_demand: Decimal
    target_demand: Decimal
    safety_stock: Decimal
    raw_quantity: Decimal
    after_minimum_order_qty: Decimal
    recommended_quantity: Decimal


class InventoryProjectionService:
    """Project inventory without persistence or external purchasing side effects."""

    def project(
        self,
        *,
        on_hand: Decimal,
        reserved: Decimal,
        forecast_points: Sequence[ForecastDemandPoint],
        scheduled_inbounds: Sequence[ScheduledInbound] = (),
        return_rate: Decimal = Decimal("0"),
    ) -> InventoryProjection:
        if on_hand < 0 or reserved < 0:
            raise ValueError("inventory_projection_negative_balance")
        if return_rate < 0 or return_rate > 1:
            raise ValueError("inventory_projection_invalid_return_rate")
        if not forecast_points:
            raise ValueError("inventory_projection_empty_forecast")

        dates = [point.business_date for point in forecast_points]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("inventory_projection_dates_not_strictly_increasing")

        inbound_by_date: dict[date, Decimal] = {}
        unknown_arrival = Decimal("0")
        first_date = dates[0]
        for inbound in scheduled_inbounds:
            if inbound.expected_arrival_date is None:
                unknown_arrival += inbound.quantity
                continue
            arrival_date = max(inbound.expected_arrival_date, first_date)
            inbound_by_date[arrival_date] = (
                inbound_by_date.get(arrival_date, Decimal("0")) + inbound.quantity
            )

        starting_available = max(Decimal("0"), on_hand - reserved)
        opening_available = starting_available
        days: list[InventoryProjectionDay] = []
        first_stockout: date | None = None
        for point in forecast_points:
            scheduled_inbound = inbound_by_date.get(point.business_date, Decimal("0"))
            return_adjustment = point.forecast_sales * return_rate
            supply = opening_available + scheduled_inbound + return_adjustment
            unmet_demand = max(Decimal("0"), point.forecast_sales - supply)
            closing_available = max(Decimal("0"), supply - point.forecast_sales)
            if first_stockout is None and unmet_demand > 0:
                first_stockout = point.business_date
            days.append(
                InventoryProjectionDay(
                    business_date=point.business_date,
                    opening_available=opening_available,
                    scheduled_inbound=scheduled_inbound,
                    return_adjustment=return_adjustment,
                    forecast_sales=point.forecast_sales,
                    closing_available=closing_available,
                    unmet_demand=unmet_demand,
                )
            )
            opening_available = closing_available

        return InventoryProjection(
            starting_available=starting_available,
            days=tuple(days),
            first_stockout_date=first_stockout,
            unknown_arrival_inbound_quantity=unknown_arrival,
        )


class ReplenishmentPlanner:
    """Create a draft replenishment quantity from an in-memory projection."""

    def __init__(self, projection_service: InventoryProjectionService | None = None):
        self.projection_service = projection_service or InventoryProjectionService()

    def draft(
        self,
        *,
        on_hand: Decimal,
        reserved: Decimal,
        forecast_points: Sequence[ForecastDemandPoint],
        scheduled_inbounds: Sequence[ScheduledInbound] = (),
        return_rate: Decimal = Decimal("0"),
        lead_time_days: int,
        review_period_days: int,
        safety_stock_days: int,
        minimum_safety_stock: Decimal,
        minimum_order_qty: Decimal,
        order_multiple: Decimal,
    ) -> ReplenishmentDraft:
        if lead_time_days < 0 or review_period_days < 0 or safety_stock_days < 0:
            raise ValueError("replenishment_negative_day_policy")
        if minimum_safety_stock < 0 or minimum_order_qty < 0 or order_multiple <= 0:
            raise ValueError("replenishment_invalid_quantity_policy")
        planning_days = lead_time_days + review_period_days
        if planning_days < 1 or len(forecast_points) < planning_days:
            raise ValueError("forecast_horizon_insufficient")

        projection = self.projection_service.project(
            on_hand=on_hand,
            reserved=reserved,
            forecast_points=forecast_points,
            scheduled_inbounds=scheduled_inbounds,
            return_rate=return_rate,
        )
        planning_projection = projection.days[:planning_days]
        lead_demand = sum(
            (day.forecast_sales for day in planning_projection[:lead_time_days]),
            Decimal("0"),
        )
        target_demand = sum(
            (day.forecast_sales for day in planning_projection), Decimal("0")
        )
        average_daily_demand = target_demand / Decimal(planning_days)
        safety_stock = max(
            minimum_safety_stock,
            average_daily_demand * Decimal(safety_stock_days),
        )
        scheduled_supply = sum(
            (
                day.scheduled_inbound + day.return_adjustment
                for day in planning_projection
            ),
            Decimal("0"),
        )
        raw_quantity = max(
            Decimal("0"),
            target_demand + safety_stock - projection.starting_available - scheduled_supply,
        )
        after_minimum = (
            max(raw_quantity, minimum_order_qty) if raw_quantity > 0 else Decimal("0")
        )
        recommended = (
            (after_minimum / order_multiple).to_integral_value(rounding=ROUND_CEILING)
            * order_multiple
            if after_minimum > 0
            else Decimal("0")
        )
        return ReplenishmentDraft(
            status="draft",
            persisted=False,
            external_order_created=False,
            projection=projection,
            lead_demand=lead_demand,
            target_demand=target_demand,
            safety_stock=safety_stock,
            raw_quantity=raw_quantity,
            after_minimum_order_qty=after_minimum,
            recommended_quantity=recommended,
        )
