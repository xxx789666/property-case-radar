"""Objective 100-point score for public land-sale listings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from scoring.sale_score import calculate_discount_rate


@dataclass(frozen=True)
class LandScoreInput:
    listing_unit_price: int
    market_unit_price: int
    transaction_count: int = 0
    completeness_ratio: float = 0
    price_drop_rate: float = 0


@dataclass(frozen=True)
class LandScore:
    total: int
    discount_rate: Decimal
    price_discount: int
    market_liquidity: int
    price_change: int
    data_completeness: int


def score_land(values: LandScoreInput) -> LandScore:
    """Score land without applying residential age/parking assumptions.

    Composition:
    - Regional actual-price discount: 50
    - District land transaction liquidity: 25
    - Recent asking-price reduction: 10
    - Listing data completeness: 15
    """

    discount = calculate_discount_rate(
        values.listing_unit_price,
        values.market_unit_price,
    )
    price_points = round(max(0, min(50, float(discount) / 0.30 * 50)))
    liquidity_points = round(
        max(0, min(25, values.transaction_count / 20 * 25))
    )
    price_change_points = round(
        max(0, min(10, values.price_drop_rate / 0.10 * 10))
    )
    completeness_points = round(
        max(0, min(1, values.completeness_ratio)) * 15
    )
    total = (
        price_points
        + liquidity_points
        + price_change_points
        + completeness_points
    )
    return LandScore(
        total=max(0, min(100, total)),
        discount_rate=discount,
        price_discount=price_points,
        market_liquidity=liquidity_points,
        price_change=price_change_points,
        data_completeness=completeness_points,
    )
