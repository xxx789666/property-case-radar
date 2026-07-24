"""Status state machine for auction cases.

Implements taiwan_real_estate_radar.md section 九's status list:

    新增公告 -> 更正公告 / 底價變更 / 拍賣日期變更 (amendments, any order)
             -> 流標 -> 轉下一拍 (新增公告 for round N+1) -> ... repeat
             -> 停拍 / 撤回 / 拍定 (terminal)

This is materially different from the sale pipeline's simpler price-
history tracking (CLAUDE.md), which is why it lives in its own module
rather than a shared "status history" abstraction.
"""

from __future__ import annotations

from datetime import date, datetime

from property_case_radar.auction.models import (
    AuctionCase,
    AuctionRound,
    AuctionStatus,
    AuctionStatusEvent,
    RoundResult,
)

# States in which the case is still an active, unresolved announcement.
ACTIVE_STATES = frozenset(
    {
        AuctionStatus.ANNOUNCED,
        AuctionStatus.CORRECTED,
        AuctionStatus.PRICE_CHANGED,
        AuctionStatus.DATE_CHANGED,
    }
)

# States in which no further transitions are accepted in this version.
# 停拍 (SUSPENDED) is modeled as terminal for v1 because the spec does not
# describe a resume-from-suspension flow; if that requirement shows up,
# add SUSPENDED -> ANNOUNCED to the table below rather than reworking it.
TERMINAL_STATES = frozenset({AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED, AuctionStatus.SUSPENDED})

# Any "active" state can receive an amendment (correction / price change /
# date change), move to failed, or resolve directly (suspended/withdrawn/
# awarded). FAILED can only advance back to ANNOUNCED (next round) or
# resolve; it cannot receive amendments directly (an amendment reopens the
# announcement, i.e. goes through ANNOUNCED first).
_ACTIVE_TARGETS = frozenset(
    {
        AuctionStatus.CORRECTED,
        AuctionStatus.PRICE_CHANGED,
        AuctionStatus.DATE_CHANGED,
        AuctionStatus.FAILED,
        AuctionStatus.SUSPENDED,
        AuctionStatus.WITHDRAWN,
        AuctionStatus.AWARDED,
    }
)

TRANSITIONS: dict[AuctionStatus, frozenset[AuctionStatus]] = {
    AuctionStatus.ANNOUNCED: _ACTIVE_TARGETS,
    AuctionStatus.CORRECTED: _ACTIVE_TARGETS,
    AuctionStatus.PRICE_CHANGED: _ACTIVE_TARGETS,
    AuctionStatus.DATE_CHANGED: _ACTIVE_TARGETS,
    AuctionStatus.FAILED: frozenset(
        {AuctionStatus.ANNOUNCED, AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED}
    ),
    AuctionStatus.SUSPENDED: frozenset(),
    AuctionStatus.WITHDRAWN: frozenset(),
    AuctionStatus.AWARDED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, from_status: AuctionStatus, to_status: AuctionStatus) -> None:
        super().__init__(f"cannot transition auction case from {from_status.value} to {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: AuctionStatus, to_status: AuctionStatus) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def apply_transition(
    case: AuctionCase,
    to_status: AuctionStatus,
    *,
    changed_at: datetime,
    note: str = "",
    next_round: AuctionRound | None = None,
    winning_price: float | None = None,
    new_floor_price_total: float | None = None,
    new_floor_unit_price: float | None = None,
    new_auction_date: date | None = None,
) -> AuctionStatusEvent:
    """Advance ``case`` to ``to_status`` in place, returning the recorded event.

    ``next_round``: required when transitioning FAILED -> ANNOUNCED, i.e.
    "流標 -> 轉下一拍" (第一拍流標/轉第二拍, 第二拍流標/轉第三拍). The
    prior round is marked FAILED (if not already) and the new round is
    appended, becoming ``case.current_round``. ``next_round.round_number``
    must be strictly greater than the round it replaces -- round numbers
    only move forward.

    ``winning_price``: REQUIRED (and must be positive) when transitioning
    to AWARDED (拍定/得標); rejected for every other ``to_status``.

    ``new_floor_price_total`` / ``new_floor_unit_price``: at least one is
    REQUIRED when transitioning to PRICE_CHANGED (底價變更); applied to
    ``case.current_round`` in place, with the previous values captured on
    the returned event so notifications.py can render the change.
    Rejected for every other ``to_status``.

    ``new_auction_date``: REQUIRED when transitioning to DATE_CHANGED
    (拍賣日期變更); applied to ``case.current_round.auction_date`` in
    place, with the previous value captured on the returned event.
    Rejected for every other ``to_status``.

    ``changed_at`` must not precede the case's current ``updated_at`` --
    status history is append-only and must move forward in time.
    """
    from_status = case.status
    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(from_status, to_status)

    if changed_at < case.updated_at:
        raise ValueError(
            f"changed_at ({changed_at!r}) must not precede the case's current "
            f"updated_at ({case.updated_at!r}); status history cannot move backwards in time"
        )

    is_round_advance = from_status == AuctionStatus.FAILED and to_status == AuctionStatus.ANNOUNCED
    if is_round_advance:
        if next_round is None:
            raise ValueError("next_round is required when advancing from a failed round to the next round")
        if case.current_round is not None and next_round.round_number <= case.current_round.round_number:
            raise ValueError(
                f"next_round.round_number ({next_round.round_number}) must be greater than "
                f"the current round_number ({case.current_round.round_number})"
            )
        if case.current_round is not None and case.current_round.result == RoundResult.PENDING:
            case.current_round.result = RoundResult.FAILED
        case.rounds.append(next_round)
    elif next_round is not None:
        raise ValueError("next_round is only accepted when advancing from FAILED to ANNOUNCED")

    previous_floor_price_total: float | None = None
    previous_floor_unit_price: float | None = None
    if to_status == AuctionStatus.PRICE_CHANGED:
        if new_floor_price_total is None and new_floor_unit_price is None:
            raise ValueError(
                "PRICE_CHANGED requires new_floor_price_total and/or new_floor_unit_price"
            )
        current = case.current_round
        if current is None:
            raise ValueError("cannot apply a price change: case has no round yet")
        if new_floor_price_total is not None:
            if new_floor_price_total <= 0:
                raise ValueError("new_floor_price_total must be positive")
            previous_floor_price_total = current.floor_price_total
            current.floor_price_total = new_floor_price_total
        if new_floor_unit_price is not None:
            if new_floor_unit_price <= 0:
                raise ValueError("new_floor_unit_price must be positive")
            previous_floor_unit_price = current.floor_unit_price
            current.floor_unit_price = new_floor_unit_price
    elif new_floor_price_total is not None or new_floor_unit_price is not None:
        raise ValueError(
            "new_floor_price_total/new_floor_unit_price are only accepted when transitioning to PRICE_CHANGED"
        )

    previous_auction_date: date | None = None
    if to_status == AuctionStatus.DATE_CHANGED:
        if new_auction_date is None:
            raise ValueError("DATE_CHANGED requires new_auction_date")
        current = case.current_round
        if current is None:
            raise ValueError("cannot apply a date change: case has no round yet")
        previous_auction_date = current.auction_date
        current.auction_date = new_auction_date
    elif new_auction_date is not None:
        raise ValueError("new_auction_date is only accepted when transitioning to DATE_CHANGED")

    if winning_price is not None and to_status != AuctionStatus.AWARDED:
        raise ValueError("winning_price is only accepted when transitioning to AWARDED")

    if to_status == AuctionStatus.FAILED and case.current_round is not None:
        case.current_round.result = RoundResult.FAILED
    elif to_status == AuctionStatus.SUSPENDED and case.current_round is not None:
        case.current_round.result = RoundResult.SUSPENDED
    elif to_status == AuctionStatus.WITHDRAWN and case.current_round is not None:
        case.current_round.result = RoundResult.WITHDRAWN
    elif to_status == AuctionStatus.AWARDED:
        if winning_price is None or winning_price <= 0:
            raise ValueError("AWARDED requires a positive winning_price")
        if case.current_round is None:
            raise ValueError("cannot award a case with no round")
        case.current_round.result = RoundResult.AWARDED
        case.current_round.winning_price = winning_price

    event = AuctionStatusEvent(
        to_status=to_status,
        changed_at=changed_at,
        from_status=from_status,
        round_number=case.round_number,
        note=note,
        previous_floor_price_total=previous_floor_price_total,
        new_floor_price_total=new_floor_price_total if to_status == AuctionStatus.PRICE_CHANGED else None,
        previous_floor_unit_price=previous_floor_unit_price,
        new_floor_unit_price=new_floor_unit_price if to_status == AuctionStatus.PRICE_CHANGED else None,
        previous_auction_date=previous_auction_date,
        new_auction_date=new_auction_date if to_status == AuctionStatus.DATE_CHANGED else None,
    )
    case.status = to_status
    case.updated_at = changed_at
    case.status_history.append(event)
    _assert_invariants(case)
    return event


def _assert_invariants(case: AuctionCase) -> None:
    """Last-line, always-on check that the bookkeeping above kept ``case`` consistent.

    Not meant to validate caller input -- the ValueErrors above already
    reject bad input with an actionable message. This exists so that a
    future change to this module which breaks the round/status invariants
    fails loudly (in tests, via pytest.raises(AssertionError)) instead of
    quietly producing a case with a dangling PENDING round or an AWARDED
    case with no winner price.
    """
    for round_ in case.rounds[:-1]:
        if round_.result == RoundResult.PENDING:
            raise AssertionError(
                f"round {round_.round_number} of case {case.case_id!r} was left PENDING "
                "after the case advanced past it"
            )
    if case.status == AuctionStatus.AWARDED:
        current = case.current_round
        if current is None or current.result != RoundResult.AWARDED:
            raise AssertionError(
                f"case {case.case_id!r} status is AWARDED but its current round result is not AWARDED"
            )
        if current.winning_price is None or current.winning_price <= 0:
            raise AssertionError(f"case {case.case_id!r} is AWARDED without a positive winning_price")
