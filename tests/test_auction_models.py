from datetime import date, datetime, timezone

import pytest

from database.models.auction import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AuctionCase,
    AuctionRound,
    AuctionStatus,
    AuctionSubscription,
    CaseType,
    OccupancyStatus,
    OwnershipType,
    RoundResult,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def sample_round() -> AuctionRound:
    # Numbers mirror taiwan_real_estate_radar.md section 六's example
    # (底價 980萬／底價單價23.2萬坪／建坪42.3坪／區域成交均價35.8萬坪 ->
    # 表面折價率 35.2%), converted to whole TWD to match
    # database.models.common.MarketPrice's unit convention.
    return AuctionRound(
        round_number=2,
        floor_price_total_twd=9_800_000,
        floor_unit_price_twd=232_000,
        auction_date=date(2026, 8, 18),
        deposit_twd=1_960_000,
    )


@pytest.fixture
def sample_case(sample_round: AuctionRound) -> AuctionCase:
    return AuctionCase(
        court_name="桃園地方法院",
        case_number="115年度司執字第XXXX號",
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        division="執股",
        case_type=CaseType.RESIDENTIAL,
        city="桃園市",
        district="中壢區",
        address="桃園市中壢區中央路一段OO號",
        announced_date=date(2026, 7, 1),
        building_area_ping=42.3,
        land_area_ping=15.6,
        ownership_ratio="全部",
        ownership_type=OwnershipType.FULL,
        occupancy_status=OccupancyStatus.VACANT_DELIVERABLE,
        occupancy_note="空屋",
        debtor="王小明",
        owner="王小明",
        announcement_url="https://court.example.gov.tw/ann/12345",
        rounds=[sample_round],
    )


# --- AuctionCase construction-time defaults -----------------------------
#
# SQLAlchemy's declarative __init__ does NOT apply mapped_column(default=...)
# until flush/INSERT -- these tests guard the __init__ overrides in
# database.models.auction that backfill Python-level defaults so business
# logic and tests can construct+use these objects without ever touching a
# session (see AuctionCase.__init__'s docstring for the full story).


def test_case_defaults_apply_without_a_session() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    assert case.status == AuctionStatus.ANNOUNCED
    assert case.ownership_type == OwnershipType.UNKNOWN
    assert case.occupancy_status == OccupancyStatus.UNKNOWN
    assert case.case_type == CaseType.OTHER
    assert case.city == ""
    assert case.has_unregistered_addition is False
    assert case.updated_at is not None


def test_ownership_type_defaults_to_unknown_not_full() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    assert case.ownership_type == OwnershipType.UNKNOWN


def test_round_result_defaults_to_pending_without_a_session() -> None:
    round_ = AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10)
    assert round_.result == RoundResult.PENDING


def test_subscription_active_defaults_to_true_without_a_session() -> None:
    sub = AuctionSubscription(discord_user_id=1)
    assert sub.active is True


# --- derived properties --------------------------------------------------


def test_current_round_and_round_number(sample_case: AuctionCase) -> None:
    assert sample_case.current_round is not None
    assert sample_case.round_number == 2


def test_current_round_none_when_no_rounds() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    assert case.current_round is None
    assert case.round_number is None


@pytest.mark.parametrize(
    "status,expected",
    [
        (OccupancyStatus.VACANT_DELIVERABLE, True),
        (OccupancyStatus.OCCUPIED_DELIVERABLE, True),
        (OccupancyStatus.NOT_DELIVERABLE, False),
        (OccupancyStatus.LEASE_EXISTS, None),
        (OccupancyStatus.THIRD_PARTY_OCCUPIED, None),
        (OccupancyStatus.UNKNOWN, None),
    ],
)
def test_is_deliverable_tri_state(status: OccupancyStatus, expected: bool | None) -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1), occupancy_status=status)
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
    assert sample_case.winning_price_twd is None
    sample_case.current_round.winning_price_twd = 10_000_000
    assert sample_case.winning_price_twd == 10_000_000


class TestIsPartialShare:
    def test_true_only_for_partial_share(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.PARTIAL_SHARE
        assert sample_case.is_partial_share is True

    def test_false_for_full_ownership(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.FULL
        assert sample_case.is_partial_share is False

    def test_stays_consistent_with_ownership_type_after_mutation(self, sample_case: AuctionCase) -> None:
        sample_case.ownership_type = OwnershipType.PARTIAL_SHARE
        assert sample_case.is_partial_share is True
        sample_case.ownership_type = OwnershipType.LAND_ONLY
        assert sample_case.is_partial_share is False


def test_active_and_terminal_statuses_are_disjoint_and_complete() -> None:
    all_statuses = set(AuctionStatus)
    assert ACTIVE_STATUSES | TERMINAL_STATUSES | {AuctionStatus.FAILED} == all_statuses
    assert ACTIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)


def test_round_is_beyond_second_round() -> None:
    assert AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10).is_beyond_second_round is False
    assert AuctionRound(round_number=2, floor_price_total_twd=100, floor_unit_price_twd=10).is_beyond_second_round is True
    assert AuctionRound(round_number=3, floor_price_total_twd=100, floor_unit_price_twd=10).is_beyond_second_round is True


class TestAuctionRoundValidation:
    def test_round_number_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="round_number"):
            AuctionRound(round_number=0, floor_price_total_twd=100, floor_unit_price_twd=10)

    def test_floor_price_total_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="floor_price_total_twd"):
            AuctionRound(round_number=1, floor_price_total_twd=0, floor_unit_price_twd=10)

    def test_floor_unit_price_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="floor_unit_price_twd"):
            AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=-10)

    def test_deposit_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="deposit_twd"):
            AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10, deposit_twd=-1)

    def test_winning_price_must_be_positive_when_provided(self) -> None:
        with pytest.raises(ValueError, match="winning_price_twd"):
            AuctionRound(round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10, winning_price_twd=0)

    def test_valid_round_construction_succeeds(self) -> None:
        round_ = AuctionRound(
            round_number=1, floor_price_total_twd=100, floor_unit_price_twd=10, deposit_twd=0, winning_price_twd=120
        )
        assert round_.deposit_twd == 0
        assert round_.winning_price_twd == 120
