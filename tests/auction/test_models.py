from datetime import date

import pytest

from property_case_radar.auction.models import (
    AuctionCase,
    AuctionFilter,
    AuctionRound,
    AuctionStatus,
    CaseType,
    OccupancyStatus,
    OwnershipType,
    RoundResult,
)


def test_current_round_and_round_number(sample_case: AuctionCase) -> None:
    assert sample_case.current_round is not None
    assert sample_case.round_number == 2


def test_current_round_none_when_no_rounds(sample_case: AuctionCase) -> None:
    sample_case.rounds = []
    assert sample_case.current_round is None
    assert sample_case.round_number is None


def test_is_deliverable_tri_state() -> None:
    for status, expected in [
        (OccupancyStatus.VACANT_DELIVERABLE, True),
        (OccupancyStatus.OCCUPIED_DELIVERABLE, True),
        (OccupancyStatus.NOT_DELIVERABLE, False),
        (OccupancyStatus.LEASE_EXISTS, None),
        (OccupancyStatus.THIRD_PARTY_OCCUPIED, None),
        (OccupancyStatus.UNKNOWN, None),
    ]:
        case = AuctionCase(
            case_id="x",
            court_name="法院",
            case_number="案號",
            first_seen_at=None,  # type: ignore[arg-type]
            updated_at=None,  # type: ignore[arg-type]
            occupancy_status=status,
        )
        assert case.is_deliverable is expected


def test_status_derived_flags(sample_case: AuctionCase) -> None:
    assert sample_case.status == AuctionStatus.ANNOUNCED
    assert not sample_case.is_suspended
    assert not sample_case.is_withdrawn
    assert not sample_case.is_failed
    assert not sample_case.is_awarded

    sample_case.status = AuctionStatus.AWARDED
    assert sample_case.is_awarded


def test_winning_price_reads_from_current_round(sample_case: AuctionCase) -> None:
    assert sample_case.winning_price is None
    sample_case.current_round.winning_price = 1000.0
    assert sample_case.winning_price == 1000.0


def test_round_is_beyond_second_round() -> None:
    assert AuctionRound(1, 100, 10).is_beyond_second_round is False
    assert AuctionRound(2, 100, 10).is_beyond_second_round is True
    assert AuctionRound(3, 100, 10).is_beyond_second_round is True


class TestAuctionRoundValidation:
    def test_round_number_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="round_number"):
            AuctionRound(round_number=0, floor_price_total=100, floor_unit_price=10)
        with pytest.raises(ValueError, match="round_number"):
            AuctionRound(round_number=-1, floor_price_total=100, floor_unit_price=10)

    def test_floor_price_total_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="floor_price_total"):
            AuctionRound(round_number=1, floor_price_total=0, floor_unit_price=10)
        with pytest.raises(ValueError, match="floor_price_total"):
            AuctionRound(round_number=1, floor_price_total=-100, floor_unit_price=10)

    def test_floor_unit_price_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="floor_unit_price"):
            AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=0)
        with pytest.raises(ValueError, match="floor_unit_price"):
            AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=-10)

    def test_deposit_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="deposit"):
            AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=10, deposit=-1)

    def test_winning_price_must_be_positive_when_provided(self) -> None:
        with pytest.raises(ValueError, match="winning_price"):
            AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=10, winning_price=0)
        with pytest.raises(ValueError, match="winning_price"):
            AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=10, winning_price=-5)

    def test_valid_round_construction_succeeds(self) -> None:
        round_ = AuctionRound(round_number=1, floor_price_total=100, floor_unit_price=10, deposit=0, winning_price=120)
        assert round_.deposit == 0
        assert round_.winning_price == 120


def test_ownership_type_defaults_to_unknown_not_full() -> None:
    case = AuctionCase(
        case_id="x",
        court_name="法院",
        case_number="案號",
        first_seen_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    assert case.ownership_type == OwnershipType.UNKNOWN


class TestIsPartialShare:
    def test_true_only_for_partial_share(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.PARTIAL_SHARE
        assert sample_case.is_partial_share is True

    def test_false_for_full_ownership(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.FULL
        assert sample_case.is_partial_share is False

    def test_false_for_unknown_ownership(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.UNKNOWN
        assert sample_case.is_partial_share is False

    def test_stays_consistent_with_ownership_type_after_mutation(self, sample_case: AuctionCase) -> None:
        # Regression guard for the old dead `is_partial_share` field: this
        # must always reflect the *current* ownership_type, never a stale
        # value set independently.
        sample_case.ownership_type = OwnershipType.PARTIAL_SHARE
        assert sample_case.is_partial_share is True
        sample_case.ownership_type = OwnershipType.LAND_ONLY
        assert sample_case.is_partial_share is False


class TestAuctionFilter:
    def test_matches_no_filters(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter().matches(sample_case)

    def test_matches_city_district(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter(city="桃園市", district="中壢區").matches(sample_case)
        assert not AuctionFilter(city="台北市").matches(sample_case)
        assert not AuctionFilter(city="桃園市", district="八德區").matches(sample_case)

    def test_matches_case_type(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter(case_type=CaseType.RESIDENTIAL).matches(sample_case)
        assert not AuctionFilter(case_type=CaseType.LAND).matches(sample_case)

    def test_matches_floor_price_max(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter(floor_price_max=1200).matches(sample_case)
        assert not AuctionFilter(floor_price_max=500).matches(sample_case)

    def test_matches_min_round(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter(min_round=2).matches(sample_case)
        assert not AuctionFilter(min_round=3).matches(sample_case)

    def test_matches_require_deliverable(self, sample_case: AuctionCase) -> None:
        assert AuctionFilter(require_deliverable=True).matches(sample_case)
        assert not AuctionFilter(require_deliverable=False).matches(sample_case)

    def test_matches_min_investment_score(self, sample_case: AuctionCase) -> None:
        assert not AuctionFilter(min_investment_score=50).matches(sample_case)  # score not yet computed
        sample_case.investment_score = 88.0
        assert AuctionFilter(min_investment_score=50).matches(sample_case)
        assert not AuctionFilter(min_investment_score=95).matches(sample_case)
