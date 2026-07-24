import pytest

from property_case_radar.auction.models import AuctionCase, AuctionRound, OccupancyStatus, OwnershipType
from property_case_radar.auction.scoring import (
    completeness_score,
    delivery_score,
    liquidity_score,
    ownership_score,
    price_discount_score,
    risk_level,
    risk_score_0_100,
    round_timing_score,
    score_case,
    surface_discount_rate,
)


def test_surface_discount_rate_matches_spec_worked_example() -> None:
    # spec section 八: 區域成交價 40 萬／坪, 法拍底價 25 萬／坪 -> 37.5%
    assert surface_discount_rate(25, 40) == pytest.approx(0.375)


def test_surface_discount_rate_matches_section_six_example(sample_case: AuctionCase) -> None:
    rate = surface_discount_rate(sample_case.current_round.floor_unit_price, 35.8)
    assert rate == pytest.approx(0.352, abs=1e-3)


def test_surface_discount_rate_rejects_non_positive_regional_average() -> None:
    with pytest.raises(ValueError):
        surface_discount_rate(10, 0)
    with pytest.raises(ValueError):
        surface_discount_rate(10, -5)


def test_price_discount_score_bounds() -> None:
    assert price_discount_score(-0.1) == 0.0
    assert price_discount_score(0.0) == 0.0
    assert price_discount_score(0.5) == 35.0
    assert price_discount_score(0.9) == 35.0  # capped


def test_delivery_score_ordering() -> None:
    assert delivery_score_case(OccupancyStatus.VACANT_DELIVERABLE) > delivery_score_case(
        OccupancyStatus.OCCUPIED_DELIVERABLE
    )
    assert delivery_score_case(OccupancyStatus.OCCUPIED_DELIVERABLE) > delivery_score_case(
        OccupancyStatus.NOT_DELIVERABLE
    )
    assert delivery_score_case(OccupancyStatus.NOT_DELIVERABLE) > delivery_score_case(
        OccupancyStatus.THIRD_PARTY_OCCUPIED
    )


def delivery_score_case(status: OccupancyStatus) -> float:
    case = AuctionCase(case_id="x", court_name="c", case_number="n", first_seen_at=None, updated_at=None, occupancy_status=status)  # type: ignore[arg-type]
    return delivery_score(case)


def test_ownership_score_partial_share_penalized_heavily() -> None:
    full = _case(ownership_type=OwnershipType.FULL)
    partial = _case(ownership_type=OwnershipType.PARTIAL_SHARE)
    assert ownership_score(full) == 20.0
    assert ownership_score(partial) < ownership_score(full) / 2


def test_ownership_score_unregistered_addition_further_penalized() -> None:
    base = _case(ownership_type=OwnershipType.FULL, has_unregistered_addition=False)
    with_addition = _case(ownership_type=OwnershipType.FULL, has_unregistered_addition=True)
    assert ownership_score(with_addition) == ownership_score(base) - 4.0


def test_ownership_score_unknown_is_conservative_not_full() -> None:
    # Unparsed/missing ownership data must never score as if it were
    # confirmed clean title -- it should sit strictly below FULL and at
    # or above the worst case (PARTIAL_SHARE), matching the "unknown ~
    # conservative middle ground" treatment used for OccupancyStatus too.
    unknown = _case(ownership_type=OwnershipType.UNKNOWN)
    full = _case(ownership_type=OwnershipType.FULL)
    partial = _case(ownership_type=OwnershipType.PARTIAL_SHARE)
    assert ownership_score(unknown) < ownership_score(full)
    assert ownership_score(unknown) > ownership_score(partial)


def test_round_timing_score_progression() -> None:
    assert round_timing_score(_case(round_number=1)) < round_timing_score(_case(round_number=2))
    assert round_timing_score(_case(round_number=2)) < round_timing_score(_case(round_number=3))
    assert round_timing_score(_case(round_number=None)) == 0.0


def test_liquidity_score_clamped() -> None:
    assert liquidity_score(0.5) == 5.0
    assert liquidity_score(-1) == 0.0
    assert liquidity_score(2) == 10.0


def test_completeness_score_full_case_near_max(sample_case: AuctionCase) -> None:
    assert completeness_score(sample_case) == pytest.approx(5.0)


def test_completeness_score_sparse_case_lower() -> None:
    sparse = AuctionCase(case_id="x", court_name="c", case_number="n", first_seen_at=None, updated_at=None)  # type: ignore[arg-type]
    assert completeness_score(sparse) < 2.0


def test_score_case_breakdown_and_total(sample_case: AuctionCase) -> None:
    breakdown = score_case(sample_case, regional_avg_unit_price=35.8, liquidity_index=0.5)
    assert breakdown.surface_discount_rate == pytest.approx(0.352, abs=1e-3)
    assert 0 <= breakdown.investment_score <= 100
    # spec example describes this exact profile (35.2% discount, round 2,
    # deliverable, full ownership) as an "88/100" strong deal; our formula
    # need not reproduce that exact figure, but should land solidly high.
    assert breakdown.investment_score >= 75


def test_score_case_requires_a_round() -> None:
    case = AuctionCase(case_id="x", court_name="c", case_number="n", first_seen_at=None, updated_at=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        score_case(case, regional_avg_unit_price=30)


def test_risk_level_thresholds() -> None:
    safe = _case(ownership_type=OwnershipType.FULL, occupancy_status=OccupancyStatus.VACANT_DELIVERABLE)
    risky = _case(ownership_type=OwnershipType.PARTIAL_SHARE, occupancy_status=OccupancyStatus.THIRD_PARTY_OCCUPIED)
    assert risk_level(safe) == "低風險"
    assert risk_level(risky) == "高風險"
    assert risk_score_0_100(safe) > risk_score_0_100(risky)


def _case(
    *,
    ownership_type: OwnershipType = OwnershipType.FULL,
    occupancy_status: OccupancyStatus = OccupancyStatus.VACANT_DELIVERABLE,
    has_unregistered_addition: bool = False,
    round_number: int | None = 2,
) -> AuctionCase:
    case = AuctionCase(
        case_id="x",
        court_name="c",
        case_number="n",
        first_seen_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
        ownership_type=ownership_type,
        occupancy_status=occupancy_status,
        has_unregistered_addition=has_unregistered_addition,
    )
    if round_number is not None:
        case.rounds = [AuctionRound(round_number=round_number, floor_price_total=100.0, floor_unit_price=10.0)]
    return case
