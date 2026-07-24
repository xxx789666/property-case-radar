from datetime import date, datetime, timezone

import pytest

from property_case_radar.auction.models import AuctionCase, AuctionRound, AuctionStatus, RoundResult
from property_case_radar.auction.state_machine import (
    InvalidTransitionError,
    _assert_invariants,
    apply_transition,
    can_transition,
)


def _t(day: int) -> datetime:
    return datetime(2026, 8, day, tzinfo=timezone.utc)


def test_can_transition_table_basic() -> None:
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.CORRECTED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.PRICE_CHANGED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.DATE_CHANGED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.FAILED)
    assert can_transition(AuctionStatus.ANNOUNCED, AuctionStatus.AWARDED)
    assert not can_transition(AuctionStatus.WITHDRAWN, AuctionStatus.ANNOUNCED)
    assert not can_transition(AuctionStatus.AWARDED, AuctionStatus.FAILED)
    assert not can_transition(AuctionStatus.SUSPENDED, AuctionStatus.ANNOUNCED)


def test_amendment_transitions(sample_case: AuctionCase) -> None:
    original_floor_total = sample_case.current_round.floor_price_total
    original_floor_unit = sample_case.current_round.floor_unit_price
    original_date = sample_case.current_round.auction_date

    apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(1), note="地址更正")
    assert sample_case.status == AuctionStatus.CORRECTED

    price_event = apply_transition(
        sample_case,
        AuctionStatus.PRICE_CHANGED,
        changed_at=_t(2),
        new_floor_price_total=784.0,
        new_floor_unit_price=18.5,
    )
    assert sample_case.status == AuctionStatus.PRICE_CHANGED
    assert sample_case.current_round.floor_price_total == 784.0
    assert sample_case.current_round.floor_unit_price == 18.5
    assert price_event.previous_floor_price_total == original_floor_total
    assert price_event.new_floor_price_total == 784.0
    assert price_event.previous_floor_unit_price == original_floor_unit
    assert price_event.new_floor_unit_price == 18.5

    date_event = apply_transition(
        sample_case, AuctionStatus.DATE_CHANGED, changed_at=_t(3), new_auction_date=date(2026, 9, 29)
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
    assert sample_case.updated_at == _t(3)


def test_price_changed_requires_at_least_one_new_value(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="PRICE_CHANGED requires"):
        apply_transition(sample_case, AuctionStatus.PRICE_CHANGED, changed_at=_t(1))


def test_price_changed_rejects_non_positive_values(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.PRICE_CHANGED, changed_at=_t(1), new_floor_price_total=0)
    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.PRICE_CHANGED, changed_at=_t(1), new_floor_unit_price=-5)


def test_date_changed_requires_new_auction_date(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="DATE_CHANGED requires"):
        apply_transition(sample_case, AuctionStatus.DATE_CHANGED, changed_at=_t(1))


def test_price_or_date_payload_rejected_outside_matching_transition(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(1), new_floor_price_total=500.0)
    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(1), new_auction_date=date(2026, 9, 1))


def test_changed_at_cannot_precede_case_updated_at(sample_case: AuctionCase) -> None:
    apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(5))
    with pytest.raises(ValueError, match="must not precede"):
        apply_transition(sample_case, AuctionStatus.DATE_CHANGED, changed_at=_t(1), new_auction_date=date(2026, 9, 1))


def test_invalid_transition_raises(sample_case: AuctionCase) -> None:
    apply_transition(sample_case, AuctionStatus.WITHDRAWN, changed_at=_t(1))
    with pytest.raises(InvalidTransitionError):
        apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2))


def test_failed_then_round_advance_requires_next_round(sample_case: AuctionCase) -> None:
    apply_transition(sample_case, AuctionStatus.FAILED, changed_at=_t(1))
    assert sample_case.current_round.result == RoundResult.FAILED

    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2))

    round3 = AuctionRound(round_number=3, floor_price_total=784.0, floor_unit_price=18.5, auction_date=date(2026, 9, 22))
    apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2), next_round=round3)
    assert sample_case.round_number == 3
    assert sample_case.status == AuctionStatus.ANNOUNCED
    assert len(sample_case.rounds) == 2


def test_full_round1_to_round3_failed_then_awarded_flow(sample_case: AuctionCase) -> None:
    # 第一拍流標 -> 轉第二拍 -> 第二拍流標 -> 轉第三拍 -> 拍定
    sample_case.rounds = [AuctionRound(round_number=1, floor_price_total=1200.0, floor_unit_price=28.4)]
    sample_case.status = AuctionStatus.ANNOUNCED

    apply_transition(sample_case, AuctionStatus.FAILED, changed_at=_t(1))
    round2 = AuctionRound(round_number=2, floor_price_total=980.0, floor_unit_price=23.2)
    apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2), next_round=round2)

    apply_transition(sample_case, AuctionStatus.FAILED, changed_at=_t(3))
    round3 = AuctionRound(round_number=3, floor_price_total=784.0, floor_unit_price=18.5)
    apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(4), next_round=round3)

    event = apply_transition(sample_case, AuctionStatus.AWARDED, changed_at=_t(5), winning_price=820.0)
    assert sample_case.status == AuctionStatus.AWARDED
    assert sample_case.is_awarded
    assert sample_case.winning_price == 820.0
    assert event.round_number == 3
    assert [r.result for r in sample_case.rounds] == [RoundResult.FAILED, RoundResult.FAILED, RoundResult.AWARDED]


def test_suspended_and_withdrawn_are_terminal(sample_case: AuctionCase) -> None:
    apply_transition(sample_case, AuctionStatus.SUSPENDED, changed_at=_t(1))
    assert sample_case.is_suspended
    assert sample_case.current_round.result == RoundResult.SUSPENDED
    with pytest.raises(InvalidTransitionError):
        apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2))


def test_next_round_rejected_outside_round_advance(sample_case: AuctionCase) -> None:
    extra_round = AuctionRound(round_number=99, floor_price_total=1.0, floor_unit_price=1.0)
    with pytest.raises(ValueError):
        apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(1), next_round=extra_round)


def test_next_round_number_must_advance(sample_case: AuctionCase) -> None:
    # sample_case starts at round 2; advancing to round 2 again or back to
    # round 1 must be rejected, only a strictly higher round number may
    # follow a FAILED round.
    apply_transition(sample_case, AuctionStatus.FAILED, changed_at=_t(1))
    same_round = AuctionRound(round_number=2, floor_price_total=900.0, floor_unit_price=21.0)
    with pytest.raises(ValueError, match="must be greater than"):
        apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2), next_round=same_round)

    earlier_round = AuctionRound(round_number=1, floor_price_total=900.0, floor_unit_price=21.0)
    with pytest.raises(ValueError, match="must be greater than"):
        apply_transition(sample_case, AuctionStatus.ANNOUNCED, changed_at=_t(2), next_round=earlier_round)


def test_awarded_requires_positive_winning_price(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="positive winning_price"):
        apply_transition(sample_case, AuctionStatus.AWARDED, changed_at=_t(1))
    with pytest.raises(ValueError, match="positive winning_price"):
        apply_transition(sample_case, AuctionStatus.AWARDED, changed_at=_t(1), winning_price=0)
    with pytest.raises(ValueError, match="positive winning_price"):
        apply_transition(sample_case, AuctionStatus.AWARDED, changed_at=_t(1), winning_price=-10)


def test_winning_price_rejected_outside_awarded(sample_case: AuctionCase) -> None:
    with pytest.raises(ValueError, match="only accepted when transitioning to AWARDED"):
        apply_transition(sample_case, AuctionStatus.CORRECTED, changed_at=_t(1), winning_price=100.0)


def test_awarded_with_no_round_is_rejected() -> None:
    case = AuctionCase(
        case_id="x",
        court_name="法院",
        case_number="案號",
        first_seen_at=_t(1),
        updated_at=_t(1),
    )
    with pytest.raises(ValueError, match="no round"):
        apply_transition(case, AuctionStatus.AWARDED, changed_at=_t(2), winning_price=100.0)


class TestInvariantGuard:
    def test_valid_case_passes(self, sample_case: AuctionCase) -> None:
        _assert_invariants(sample_case)  # should not raise

    def test_dangling_pending_round_is_rejected(self, sample_case: AuctionCase) -> None:
        # Simulate a bug that appended a second round without resolving
        # the first one -- bypasses apply_transition on purpose to prove
        # the guard actually catches this rather than trusting callers.
        sample_case.rounds.append(AuctionRound(round_number=3, floor_price_total=500.0, floor_unit_price=12.0))
        with pytest.raises(AssertionError, match="PENDING"):
            _assert_invariants(sample_case)

    def test_awarded_status_without_awarded_round_is_rejected(self, sample_case: AuctionCase) -> None:
        sample_case.status = AuctionStatus.AWARDED  # bypass apply_transition
        with pytest.raises(AssertionError, match="AWARDED"):
            _assert_invariants(sample_case)

    def test_awarded_round_without_winning_price_is_rejected(self, sample_case: AuctionCase) -> None:
        sample_case.status = AuctionStatus.AWARDED
        sample_case.current_round.result = RoundResult.AWARDED
        with pytest.raises(AssertionError, match="winning_price"):
            _assert_invariants(sample_case)
