from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RentalScoreInput:
    monthly_rent_twd: int
    rent_per_ping_twd: int
    district_median_rent_per_ping_twd: int
    completeness_ratio: float
    feature_count: int
    price_drop_rate: float = 0


@dataclass(frozen=True)
class RentalScore:
    total: int
    discount_rate: Decimal


def score_rental(value: RentalScoreInput) -> RentalScore:
    median = max(1, value.district_median_rent_per_ping_twd)
    discount = Decimal(median - value.rent_per_ping_twd) / Decimal(median)
    # 40 points: below-district asking rent per ping. Median receives 20;
    # a 25% discount receives the full component.
    price_score = max(0, min(40, round(20 + float(discount) * 80)))
    # 20 points: absolute monthly affordability.
    if value.monthly_rent_twd <= 15_000:
        affordability = 20
    elif value.monthly_rent_twd >= 60_000:
        affordability = 0
    else:
        affordability = round((60_000 - value.monthly_rent_twd) / 45_000 * 20)
    completeness = max(0, min(15, round(value.completeness_ratio * 15)))
    features = max(0, min(15, value.feature_count * 3))
    price_drop = max(0, min(10, round(value.price_drop_rate * 100)))
    return RentalScore(
        total=max(0, min(100, price_score + affordability + completeness + features + price_drop)),
        discount_rate=discount,
    )
