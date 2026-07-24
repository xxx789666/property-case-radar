from dataclasses import dataclass
from datetime import date
from statistics import fmean


@dataclass(frozen=True)
class ActualTransactionRecord:
    city: str
    district: str
    transaction_date: date
    unit_price_per_ping_twd: int
    total_price_twd: int
    building_area_ping: float
    address: str | None = None
    building_type: str | None = None


@dataclass(frozen=True)
class MarketPriceSummary:
    city: str
    district: str
    building_type: str
    average_unit_price_twd: int
    transaction_count: int
    period_start: date
    period_end: date


def summarize_market(records: list[ActualTransactionRecord]) -> MarketPriceSummary:
    if not records:
        raise ValueError("at least one transaction is required")
    first = records[0]
    if any((r.city, r.district) != (first.city, first.district) for r in records):
        raise ValueError("records must belong to the same city and district")
    return MarketPriceSummary(
        city=first.city,
        district=first.district,
        building_type=first.building_type or "住宅",
        average_unit_price_twd=round(fmean(r.unit_price_per_ping_twd for r in records)),
        transaction_count=len(records),
        period_start=min(r.transaction_date for r in records),
        period_end=max(r.transaction_date for r in records),
    )
