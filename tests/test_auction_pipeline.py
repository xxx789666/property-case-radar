from datetime import date, datetime, timezone

import pytest

from apps.services.auction_pipeline import (
    IngestOutcome,
    InvalidTransitionError,
    _assert_invariants,
    apply_status_transition,
    can_transition,
    ingest_auction_announcement,
    ingest_auction_announcements,
    rescore_case,
)
from crawlers.auction.court_crawler import FixtureAuctionAnnouncementSource
from crawlers.auction.parser import CourtAnnouncementParser
from database.models.auction import AuctionCase, AuctionRound, AuctionStatus, RoundResult
from database.models.common import MarketPrice
from database.repositories.auction import AuctionRepository


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def sample_round() -> AuctionRound:
    return AuctionRound(
        round_number=2,
        floor_price_total_twd=9_800_000,
        floor_unit_price_twd=232_000,
        auction_date=date(2026, 8, 18),
    )


@pytest.fixture
def sample_case(sample_round: AuctionRound) -> AuctionCase:
    return AuctionCase(
        court_name="桃園地方法院",
        case_number="115年度司執字第XXXX號",
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        rounds=[sample_round],
    )


# --- transition table ----------------------------------------------------


def test_can_transition_table_basic() -> None:
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.CORRECTED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.PRICE_CHANGED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.DATE_CHANGED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.FAILED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.AWARDED)
    assert not can_transition(AuctionStatus.WITHDRAWN, AuctionStatus.ANNOUNCED)
    assert not can_transition(AuctionStatus.AWARDED, AuctionStatus.FAILED)
    assert not can_transition(AuctionStatus.SUSPENDED, AuctionStatus.ANNOUNCED)


# --- amendment transitions + price/date payload ---------------------------


def test_amendment_transitions_and_payload_capture(sample_case: AuctionCase) -> None:
    original_floor_total = sample_case.current_round.floor_price_total_twd
    original_floor_unit = sample_case.current_round.floor_unit_price_twd
    original_date = sample_case.current_round.auction_date

    apply_status_transition(sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 1), note="地址更正")
    assert sample_case.status == AuctionStatus.CORRECTED

    price_event = apply_status_transition(
        sample_case,
        AuctionStatus.PRICE_CHANGED,
        changed_at=utc(2026, 8, 2),
        new_floor_price_total_twd=7_840_000,
        new_floor_unit_price_twd=185_000,
    )
    assert sample_case.status == AuctionStatus.PRICE_CHANGED
    assert sample_case.current_round.floor_price_total_twd == 7_840_000
    assert sample_case.current_round.floor_unit_price_twd == 185_000
    assert price_event.previous_floor_price_total_twd == original_floor_total
    assert price_event.new_floor_price_total_twd == 7_840_000
    assert price_event.previous_floor_unit_price_twd == original_floor_unit
    assert price_event.new_floor_unit_price_twd == 185_000

    date_event = apply_status_transition(
        sample_case, AuctionStatus.DATE_CHANGED, changed_at=utc(2026, 8, 3), new_auction_date=date(2026, 9, 29)
    )
    assert sample_case.status == AuctionStatus.DATE_CHANGED
    assert sample_case.current_round.auction_date == date(2026, 9, 29)
    assert date_event.previous_auction_date == original_date
    assert date_event.new_auction_date == date(2026, 9, 29)

    assert [e.to_status for e in sample_case.status_history] == [
        AuctionStatus.CORRECTED,
        AuctionStatus.PRICE_CHANGED,
        AuctionStatus.DATE_CHANGED,
    ]
    assert sample_case.updated_at == utc(2026, 8, 3)


def test_price_changed_requires_at_least_one_new_value(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="PRICE_CHANGED requires"):
        apply_status_transition(sample_case, AuctionStatus.PRICE_CHANGED, changed_at=utc(2026, 8, 1))


def test_price_changed_rejects_non_positive_values(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError):
        apply_status_transition(
            sample_case, AuctionStatus.PRICE_CHANGED, changed_at=utc(2026, 8, 1), new_floor_price_total_twd=0
        )


def test_date_changed_requires_new_auction_date(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="DATE_CHANGED requires"):
        apply_status_transition(sample_case, AuctionStatus.DATE_CHANGED, changed_at=utc(2026, 8, 1))


def test_price_or_date_payload_rejected_outside_matching_transition(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError):
        apply_status_transition(
            sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 1), new_floor_price_total_twd=500
        )
    with pytest.raises(ValueError):
        apply_status_transition(
            sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 1), new_auction_date=date(2026, 9, 1)
        )


def test_changed_at_cannot_precede_case_updated_at(sample_case: AuctionCase) -> None:
    apply_status_transition(sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 5))
    with pytest.raises(ValueError, match="must not precede"):
        apply_status_transition(
            sample_case, AuctionStatus.DATE_CHANGED, changed_at=utc(2026, 8, 1), new_auction_date=date(2026, 9, 1)
        )


def test_invalid_transition_raises(sample_case: AuctionCase) -> None:
    apply_status_transition(sample_case, AuctionStatus.WITHDRAWN, changed_at=utc(2026, 8, 1))
    with pytest.raises(InvalidTransitionError):
        apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2))


# --- round advance ---------------------------------------------------------


def test_failed_then_round_advance_requires_next_round(sample_case: AuctionCase) -> None:
    apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 1))
    assert sample_case.current_round.result == RoundResult.FAILED

    with pytest.raises(ValueError):
        apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2))

    round3 = AuctionRound(round_number=3, floor_price_total_twd=7_840_000, floor_unit_price_twd=185_000)
    apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2), next_round=round3)
    assert sample_case.round_number == 3
    assert sample_case.status == AuctionStatus.ANNOUNCED
    assert len(sample_case.rounds) == 2


def test_next_round_number_must_advance(sample_case: AuctionCase) -> None:
    apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 1))
    same_round = AuctionRound(round_number=2, floor_price_total_twd=1, floor_unit_price_twd=1)
    with pytest.raises(ValueError, match="must be exactly"):
        apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2), next_round=same_round)


def test_next_round_number_rejects_a_gap(sample_case: AuctionCase) -> None:
    # sample_case is at round 2; round 4 skips round 3 entirely.
    apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 1))
    gapped_round = AuctionRound(round_number=4, floor_price_total_twd=1, floor_unit_price_twd=1)
    with pytest.raises(ValueError, match="must be exactly 3"):
        apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2), next_round=gapped_round)


def test_next_round_rejected_outside_round_advance(sample_case: AuctionCase) -> None:
    extra_round = AuctionRound(round_number=99, floor_price_total_twd=1, floor_unit_price_twd=1)
    with pytest.raises(ValueError):
        apply_status_transition(sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 1), next_round=extra_round)


def test_full_round1_to_round3_failed_then_awarded_flow(sample_case: AuctionCase) -> None:
    sample_case.rounds = [AuctionRound(round_number=1, floor_price_total_twd=12_000_000, floor_unit_price_twd=284_000)]
    sample_case.status = AuctionStatus.ANNOUNCED

    apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 1))
    round2 = AuctionRound(round_number=2, floor_price_total_twd=9_800_000, floor_unit_price_twd=232_000)
    apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2), next_round=round2)

    apply_status_transition(sample_case, AuctionStatus.FAILED, changed_at=utc(2026, 8, 3))
    round3 = AuctionRound(round_number=3, floor_price_total_twd=7_840_000, floor_unit_price_twd=185_000)
    apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 4), next_round=round3)

    event = apply_status_transition(sample_case, AuctionStatus.AWARDED, changed_at=utc(2026, 8, 5), winning_price_twd=8_200_000)
    assert sample_case.status == AuctionStatus.AWARDED
    assert sample_case.is_awarded
    assert sample_case.winning_price_twd == 8_200_000
    assert event.round_number == 3
    assert [r.result for r in sample_case.rounds] == [RoundResult.FAILED, RoundResult.FAILED, RoundResult.AWARDED]


def test_suspended_and_withdrawn_are_terminal(sample_case: AuctionCase) -> None:
    apply_status_transition(sample_case, AuctionStatus.SUSPENDED, changed_at=utc(2026, 8, 1))
    assert sample_case.is_suspended
    assert sample_case.current_round.result == RoundResult.SUSPENDED
    with pytest.raises(InvalidTransitionError):
        apply_status_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=utc(2026, 8, 2))


def test_awarded_requires_positive_winning_price(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="positive winning_price"):
        apply_status_transition(sample_case, AuctionStatus.AWARDED, changed_at=utc(2026, 8, 1))


def test_winning_price_rejected_outside_awarded(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="only accepted when transitioning to AWARDED"):
        apply_status_transition(sample_case, AuctionStatus.CORRECTED, changed_at=utc(2026, 8, 1), winning_price_twd=100)


def test_awarded_with_no_round_is_rejected() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 8, 1), updated_at=utc(2026, 8, 1))
    with pytest.raises(ValueError, match="no round"):
        apply_status_transition(case, AuctionStatus.AWARDED, changed_at=utc(2026, 8, 2), winning_price_twd=100)


class TestInvariantGuard:
    def test_valid_case_passes(self, sample_case: AuctionCase) -> None:
        _assert_invariants(sample_case)

    def test_dangling_pending_round_is_rejected(self, sample_case: AuctionCase) -> None:
        sample_case.rounds.append(AuctionRound(round_number=3, floor_price_total_twd=500, floor_unit_price_twd=12))
        with pytest.raises(AssertionError, match="PENDING"):
            _assert_invariants(sample_case)

    def test_awarded_status_without_awarded_round_is_rejected(self, sample_case: AuctionCase) -> None:
        sample_case.status = AuctionStatus.AWARDED
        with pytest.raises(AssertionError, match="AWARDED"):
            _assert_invariants(sample_case)

    def test_awarded_round_without_winning_price_is_rejected(self, sample_case: AuctionCase) -> None:
        sample_case.status = AuctionStatus.AWARDED
        sample_case.current_round.result = RoundResult.AWARDED
        with pytest.raises(AssertionError, match="winning_price"):
            _assert_invariants(sample_case)


# --- ingestion glue --------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_creates_case_with_full_chronological_history(session_factory) -> None:
    with session_factory() as session:
        session.add(
            MarketPrice(
                city="桃園市", district="中壢區", building_type="住宅", average_unit_price_twd=358_000, transaction_count=40
            )
        )
        session.commit()
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()

        result = await ingest_auction_announcements(source, parser, session)
        # 9 fixtures now cover 4 distinct cases (see
        # test_auction_pipeline_end_to_end.py for the FAILED/SUSPENDED/
        # WITHDRAWN/AWARDED-specific cases); this test only cares about
        # the original 桃園 case's chronological history.
        assert result.processed == 9
        assert result.created == 4
        assert result.skipped_unchanged == 0

        case = AuctionRepository(session).get_by_case_number("桃園地方法院", "115年度司執字第12345號")
        assert case is not None
        assert case.status == AuctionStatus.DATE_CHANGED
        assert case.round_number == 2
        assert case.current_round.floor_price_total_twd == 7_840_000
        assert case.investment_score is not None
        history = sorted(case.status_history, key=lambda e: e.changed_at)
        assert [e.to_status for e in history] == [
            AuctionStatus.ANNOUNCED,
            AuctionStatus.CORRECTED,
            AuctionStatus.PRICE_CHANGED,
            AuctionStatus.DATE_CHANGED,
        ]
        assert [e.changed_at.date() for e in history] == [
            date(2026, 7, 10),
            date(2026, 7, 15),
            date(2026, 8, 25),
            date(2026, 9, 1),
        ]


@pytest.mark.asyncio
async def test_ingest_twice_is_idempotent(session_factory) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        first = await ingest_auction_announcements(source, parser, session)
        second = await ingest_auction_announcements(source, parser, session)
        assert first.created == 4
        assert second.created == 0
        assert second.status_changed == 0
        assert second.skipped_unchanged == 9


@pytest.mark.asyncio
async def test_ingest_without_market_price_leaves_score_uncached(session_factory) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        await ingest_auction_announcements(source, parser, session)
        case = AuctionRepository(session).get_by_case_number("桃園地方法院", "115年度司執字第12345號")
        assert case.investment_score is None


def test_rescore_case_returns_none_without_round() -> None:
    case = AuctionCase(court_name="c", case_number="n", first_seen_at=utc(2026, 1, 1))
    assert rescore_case(case, session=None) is None  # type: ignore[arg-type]
