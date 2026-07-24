from datetime import datetime, timezone
from decimal import Decimal

import pytest

from database.models.auction import AuctionCase, AuctionRound, OccupancyStatus, OwnershipType
from scoring.auction_score import (
    calculate_surface_discount_rate,
    completeness_score,
    delivery_score,
    liquidity_score,
    ownership_score,
    price_discount_score,
    risk_level,
    risk_score_0_100,
    round_timing_score,
    score_auction_case,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_surface_discount_rate_matches_spec_worked_example() -> None:
    # spec section 八: 區域成交價 40 萬／坪, 法拍底價 25 萬／坪 -> 37.5%
    assert calculate_surface_discount_rate(250_000, 400_000) == Decimal("0.3750")


def test_surface_discount_rate_rejects_non_positive_market_price() -> None:
    with pytest.raises(ValueError):
        calculate_surface_discount_rate(10, 0)
    with pytest.raises(ValueError):
        calculate_surface_discount_rate(10, -5)


def test_price_discount_score_bounds() -> None:
    assert price_discount_score(Decimal("-0.1")) == 0
    assert price_discount_score(Decimal("0")) == 0
    assert price_discount_score(Decimal("0.5")) == 35
    assert price_discount_score(Decimal("0.9")) == 35  # capped


def _case(
    *,
    ownership_type: OwnershipType = OwnershipType.FULL,
    occupancy_status: OccupancyStatus = OccupancyStatus.VACANT_DELIVERABLE,
    has_unregistered_addition: bool = False,
    round_number: int | None = 2,
    documents=None,
    **extra,
) -> AuctionCase:
    case = AuctionCase(
        court_name="c",
        case_number="n",
        first_seen_at=utc(2026, 1, 1),
        ownership_type=ownership_type,
        occupancy_status=occupancy_status,
        has_unregistered_addition=has_unregistered_addition,
        **extra,
    )
    if round_number is not None:
        case.rounds = [AuctionRound(round_number=round_number, floor_price_total_twd=100, floor_unit_price_twd=10)]
    return case


def test_delivery_score_ordering() -> None:
    assert delivery_score(_case(occupancy_status=OccupancyStatus.VACANT_DELIVERABLE)) > delivery_score(
        _case(occupancy_status=OccupancyStatus.OCCUPIED_DELIVERABLE)
    )
    assert delivery_score(_case(occupancy_status=OccupancyStatus.OCCUPIED_DELIVERABLE)) > delivery_score(
        _case(occupancy_status=OccupancyStatus.NOT_DELIVERABLE)
    )
    assert delivery_score(_case(occupancy_status=OccupancyStatus.NOT_DELIVERABLE)) > delivery_score(
        _case(occupancy_status=OccupancyStatus.THIRD_PARTY_OCCUPIED)
    )


def test_ownership_score_partial_share_penalized_heavily() -> None:
    full = _case(ownership_type=OwnershipType.FULL)
    partial = _case(ownership_type=OwnershipType.PARTIAL_SHARE)
    assert ownership_score(full) == 20
    assert ownership_score(partial) < ownership_score(full) / 2


def test_ownership_score_unregistered_addition_further_penalized() -> None:
    base = _case(ownership_type=OwnershipType.FULL, has_unregistered_addition=False)
    with_addition = _case(ownership_type=OwnershipType.FULL, has_unregistered_addition=True)
    assert ownership_score(with_addition) == ownership_score(base) - 4


def test_ownership_score_unknown_is_conservative_not_full() -> None:
    unknown = _case(ownership_type=OwnershipType.UNKNOWN)
    full = _case(ownership_type=OwnershipType.FULL)
    partial = _case(ownership_type=OwnershipType.PARTIAL_SHARE)
    assert ownership_score(unknown) < ownership_score(full)
    assert ownership_score(unknown) > ownership_score(partial)


def test_round_timing_score_progression() -> None:
    assert round_timing_score(_case(round_number=1)) < round_timing_score(_case(round_number=2))
    assert round_timing_score(_case(round_number=2)) < round_timing_score(_case(round_number=3))
    assert round_timing_score(_case(round_number=None)) == 0


def test_liquidity_score_clamped() -> None:
    assert liquidity_score(0.5) == 5
    assert liquidity_score(-1) == 0
    assert liquidity_score(2) == 10


def test_completeness_score_full_case_near_max() -> None:
    from datetime import date

    from database.models.auction import AuctionDocument

    case = _case(
        building_area_ping=Decimal("42.3"),
        land_area_ping=Decimal("15.6"),
        ownership_ratio="全部",
        announcement_url="https://x",
        announced_date=date(2026, 7, 1),
    )
    case.documents.append(AuctionDocument(doc_type="announcement", url="https://x", content_hash="abc"))
    assert completeness_score(case) == 5


def test_completeness_score_sparse_case_lower() -> None:
    sparse = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    assert completeness_score(sparse) < 2


def test_score_auction_case_breakdown_and_total() -> None:
    case = _case(building_area_ping=Decimal("42.3"))
    case.rounds = [AuctionRound(round_number=2, floor_price_total_twd=9_800_000, floor_unit_price_twd=232_000)]
    breakdown = score_auction_case(case, regional_avg_unit_price_twd=358_000, liquidity_index=0.5)
    assert breakdown.surface_discount_rate == pytest.approx(Decimal("0.3520"), abs=Decimal("0.001"))
    assert 0 <= breakdown.total <= 100
    assert breakdown.total >= 75


def test_score_auction_case_requires_a_round() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    with pytest.raises(ValueError):
        score_auction_case(case, regional_avg_unit_price_twd=300_000)


def test_risk_level_thresholds() -> None:
    safe = _case(ownership_type=OwnershipType.FULL, occupancy_status=OccupancyStatus.VACANT_DELIVERABLE)
    risky = _case(ownership_type=OwnershipType.PARTIAL_SHARE, occupancy_status=OccupancyStatus.THIRD_PARTY_OCCUPIED)
    assert risk_level(safe) == "低風險"
    assert risk_level(risky) == "高風險"
    assert risk_score_0_100(safe) > risk_score_0_100(risky)
