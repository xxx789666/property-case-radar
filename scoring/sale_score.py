from dataclasses import dataclass
from decimal import Decimal


def calculate_discount_rate(listing_unit_price: int, market_unit_price: int) -> Decimal:
    if listing_unit_price <= 0 or market_unit_price <= 0:
        raise ValueError("unit prices must be positive")
    return (Decimal(1) - Decimal(listing_unit_price) / Decimal(market_unit_price)).quantize(
        Decimal("0.0001")
    )


@dataclass(frozen=True)
class SaleScoreInput:
    listing_unit_price: int
    market_unit_price: int
    transaction_count: int = 0
    age_years: float | None = None
    has_parking: bool | None = None
    completeness_ratio: float = 0
    price_drop_rate: float = 0


@dataclass(frozen=True)
class SaleScore:
    total: int
    discount_rate: Decimal
    price_discount: int
    market_liquidity: int
    property_condition: int
    data_completeness: int
    price_change: int


def score_sale(values: SaleScoreInput) -> SaleScore:
    discount = calculate_discount_rate(values.listing_unit_price, values.market_unit_price)
    price_points = round(max(0, min(40, float(discount) / 0.30 * 40)))
    liquidity_points = round(max(0, min(20, values.transaction_count / 30 * 20)))

    age_points = 0
    if values.age_years is not None:
        age_points = round(max(0, min(15, (40 - values.age_years) / 40 * 15)))
    parking_points = 5 if values.has_parking else 0
    condition_points = min(20, age_points + parking_points)

    completeness_points = round(max(0, min(1, values.completeness_ratio)) * 10)
    price_change_points = round(max(0, min(10, values.price_drop_rate / 0.10 * 10)))
    total = price_points + liquidity_points + condition_points + completeness_points + price_change_points
    return SaleScore(
        total=max(0, min(100, total)),
        discount_rate=discount,
        price_discount=price_points,
        market_liquidity=liquidity_points,
        property_condition=condition_points,
        data_completeness=completeness_points,
        price_change=price_change_points,
    )
