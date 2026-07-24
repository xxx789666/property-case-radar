"""Auction status state machine + announcement ingestion.

Implements taiwan_real_estate_radar.md section 九's status list:

    新增公告 -> 更正公告 / 底價變更 / 拍賣日期變更 (amendments, any order)
             -> 流標 -> 轉下一拍 (新增公告 for round N+1) -> ... repeat
             -> 停拍 / 撤回 / 拍定 (terminal)

This is materially different from the sale pipeline's simpler price-
history tracking (CLAUDE.md), which is why the transition logic lives
here (mirroring how apps.services.sale_pipeline.ingest_sale_listings
owns "what happens on ingest" for the sale side) rather than being
folded into database.repositories.auction, which stays a thin
persistence layer.

``apply_status_transition`` and ``_assert_invariants`` are a straight
port of a standalone auction-vertical prototype's state machine (same
invariants, same tests ported to tests/test_auction_pipeline.py); only
the object types changed, from a plain dataclass to this SQLAlchemy ORM
model, and monetary fields to whole TWD (``*_twd``) to match
``database.models.common.MarketPrice``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.auction.court_crawler import AuctionAnnouncementSource
from crawlers.auction.document_parser import extract_documents, is_unchanged
from crawlers.auction.parser import AnnouncementKind, CourtAnnouncementParser, ParsedAnnouncement
from database.models.auction import (
    ACTIVE_STATUSES,
    CASE_TYPE_LABELS,
    AuctionCase,
    AuctionDocument,
    AuctionRound,
    AuctionStatus,
    AuctionStatusHistory,
    RoundResult,
)
from apps.services.auction_notification_outbox import queue_pending_notifications
from database.models.common import MarketPrice
from database.repositories.auction import AuctionRepository
from scoring.auction_score import AuctionScore, risk_score_0_100, score_auction_case

logger = logging.getLogger(__name__)

# --- status transition table ------------------------------------------
#
# Any "active" state can receive an amendment (correction / price change /
# date change), move to failed, or resolve directly (suspended/withdrawn/
# awarded). FAILED can only advance back to ANNOUNCED (next round) or
# resolve; it cannot receive amendments directly (an amendment reopens the
# announcement, i.e. goes through ANNOUNCED first). SUSPENDED is modeled
# as terminal for v1 because the spec does not describe a resume-from-
# suspension flow; if that requirement shows up, add
# SUSPENDED -> ANNOUNCED here rather than reworking the rest.

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


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware.

    SQLite -- unlike PostgreSQL's real ``TIMESTAMPTZ`` -- does not
    preserve ``tzinfo`` across an actual round trip through the database,
    even for a ``DateTime(timezone=True)`` column: a freshly-constructed,
    still-in-memory ORM object keeps the aware datetime you gave it, but
    once the session expires it and reloads from SQLite (e.g. after a
    commit, in tests), the same attribute comes back naive. Comparing an
    aware ``changed_at`` against a naive ``case.updated_at`` in that case
    raises ``TypeError`` instead of evaluating the invariant. Every
    caller-supplied timestamp in this module is UTC by convention
    (``database.models.base.utcnow``), so naive values are assumed to
    already be UTC rather than local time.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def apply_status_transition(
    case: AuctionCase,
    to_status: AuctionStatus,
    *,
    changed_at: datetime,
    note: str = "",
    next_round: AuctionRound | None = None,
    winning_price_twd: int | None = None,
    new_floor_price_total_twd: int | None = None,
    new_floor_unit_price_twd: int | None = None,
    new_auction_date: date | None = None,
) -> AuctionStatusHistory:
    """Advance ``case`` to ``to_status`` in place, returning the recorded event.

    ``next_round``: required when transitioning FAILED -> ANNOUNCED, i.e.
    "流標 -> 轉下一拍". The prior round is marked FAILED (if not already)
    and the new round is appended. ``next_round.round_number`` must be
    exactly one more than the round it replaces -- no gaps or skips.

    ``winning_price_twd``: REQUIRED (and must be positive -- enforced by
    ``AuctionRound``'s own validator) when transitioning to AWARDED
    (拍定/得標); rejected for every other ``to_status``.

    ``new_floor_price_total_twd`` / ``new_floor_unit_price_twd``: at
    least one is REQUIRED when transitioning to PRICE_CHANGED (底價變
    更); applied to ``case.current_round`` in place (validated positive
    by ``AuctionRound``), with the previous values captured on the
    returned event. Rejected for every other ``to_status``.

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

    changed_at = _as_aware_utc(changed_at)
    if changed_at < _as_aware_utc(case.updated_at):
        raise ValueError(
            f"changed_at ({changed_at!r}) must not precede the case's current "
            f"updated_at ({case.updated_at!r}); status history cannot move backwards in time"
        )

    is_round_advance = from_status == AuctionStatus.FAILED and to_status == AuctionStatus.ANNOUNCED
    if is_round_advance:
        if next_round is None:
            raise ValueError("next_round is required when advancing from a failed round to the next round")
        if case.current_round is not None and next_round.round_number != case.current_round.round_number + 1:
            raise ValueError(
                f"next_round.round_number ({next_round.round_number}) must be exactly "
                f"{case.current_round.round_number + 1} (the next round); no gaps or skips are allowed"
            )
        if case.current_round is not None and case.current_round.result == RoundResult.PENDING:
            case.current_round.result = RoundResult.FAILED
        case.rounds.append(next_round)
    elif next_round is not None:
        raise ValueError("next_round is only accepted when advancing from FAILED to ANNOUNCED")

    previous_floor_price_total_twd: int | None = None
    previous_floor_unit_price_twd: int | None = None
    if to_status == AuctionStatus.PRICE_CHANGED:
        if new_floor_price_total_twd is None and new_floor_unit_price_twd is None:
            raise ValueError("PRICE_CHANGED requires new_floor_price_total_twd and/or new_floor_unit_price_twd")
        current = case.current_round
        if current is None:
            raise ValueError("cannot apply a price change: case has no round yet")
        if new_floor_price_total_twd is not None:
            previous_floor_price_total_twd = current.floor_price_total_twd
            current.floor_price_total_twd = new_floor_price_total_twd  # validated by AuctionRound
        if new_floor_unit_price_twd is not None:
            previous_floor_unit_price_twd = current.floor_unit_price_twd
            current.floor_unit_price_twd = new_floor_unit_price_twd  # validated by AuctionRound
    elif new_floor_price_total_twd is not None or new_floor_unit_price_twd is not None:
        raise ValueError(
            "new_floor_price_total_twd/new_floor_unit_price_twd are only accepted when transitioning to PRICE_CHANGED"
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

    if winning_price_twd is not None and to_status != AuctionStatus.AWARDED:
        raise ValueError("winning_price_twd is only accepted when transitioning to AWARDED")

    if to_status == AuctionStatus.FAILED and case.current_round is not None:
        case.current_round.result = RoundResult.FAILED
    elif to_status == AuctionStatus.SUSPENDED and case.current_round is not None:
        case.current_round.result = RoundResult.SUSPENDED
    elif to_status == AuctionStatus.WITHDRAWN and case.current_round is not None:
        case.current_round.result = RoundResult.WITHDRAWN
    elif to_status == AuctionStatus.AWARDED:
        if winning_price_twd is None:
            raise ValueError("AWARDED requires a positive winning_price_twd")
        if case.current_round is None:
            raise ValueError("cannot award a case with no round")
        case.current_round.result = RoundResult.AWARDED
        case.current_round.winning_price_twd = winning_price_twd  # validated positive by AuctionRound

    event = AuctionStatusHistory(
        to_status=to_status,
        changed_at=changed_at,
        from_status=from_status,
        round_number=case.round_number,
        note=note,
        previous_floor_price_total_twd=previous_floor_price_total_twd,
        new_floor_price_total_twd=new_floor_price_total_twd if to_status == AuctionStatus.PRICE_CHANGED else None,
        previous_floor_unit_price_twd=previous_floor_unit_price_twd,
        new_floor_unit_price_twd=new_floor_unit_price_twd if to_status == AuctionStatus.PRICE_CHANGED else None,
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

    Not meant to validate caller input -- the ValueErrors above (and
    AuctionRound's own ``@validates``) already reject bad input with an
    actionable message. This exists so a future change to this module
    which breaks the round/status invariants fails loudly (in tests, via
    ``pytest.raises(AssertionError)``) instead of quietly producing a
    case with a dangling PENDING round or an AWARDED case with no winner
    price.
    """
    for round_ in case.rounds[:-1]:
        if round_.result == RoundResult.PENDING:
            raise AssertionError(
                f"round {round_.round_number} of case {case.case_number!r} was left PENDING "
                "after the case advanced past it"
            )
    if case.status == AuctionStatus.AWARDED:
        current = case.current_round
        if current is None or current.result != RoundResult.AWARDED:
            raise AssertionError(
                f"case {case.case_number!r} status is AWARDED but its current round result is not AWARDED"
            )
        if current.winning_price_twd is None or current.winning_price_twd <= 0:
            raise AssertionError(f"case {case.case_number!r} is AWARDED without a positive winning_price_twd")


# --- announcement ingestion --------------------------------------------


def _apply_descriptive_fields(case: AuctionCase, parsed: ParsedAnnouncement) -> None:
    """Copy the non-round, non-status descriptive fields from a parsed announcement onto ``case``."""
    case.division = parsed.division
    case.case_type = parsed.case_type
    case.city = parsed.city
    case.district = parsed.district
    case.address = parsed.address
    case.announced_date = parsed.announced_date
    case.building_area_ping = parsed.building_area_ping
    case.land_area_ping = parsed.land_area_ping
    case.ownership_ratio = parsed.ownership_ratio
    case.ownership_type = parsed.ownership_type
    case.occupancy_status = parsed.occupancy_status
    case.occupancy_note = parsed.occupancy_note
    case.debtor = parsed.debtor
    case.owner = parsed.owner
    case.lease_status = parsed.lease_status
    case.seizure_status = parsed.seizure_status
    case.other_encumbrances = parsed.other_encumbrances
    case.building_use = parsed.building_use
    case.zoning = parsed.zoning
    case.has_unregistered_addition = parsed.has_unregistered_addition
    case.announcement_url = parsed.announcement_url


_KIND_TO_STATUS: dict[AnnouncementKind, AuctionStatus] = {
    AnnouncementKind.NEW: AuctionStatus.ANNOUNCED,
    AnnouncementKind.CORRECTION: AuctionStatus.CORRECTED,
    AnnouncementKind.PRICE_CHANGE: AuctionStatus.PRICE_CHANGED,
    AnnouncementKind.DATE_CHANGE: AuctionStatus.DATE_CHANGED,
    AnnouncementKind.FAILED: AuctionStatus.FAILED,
    AnnouncementKind.SUSPENDED: AuctionStatus.SUSPENDED,
    AnnouncementKind.WITHDRAWN: AuctionStatus.WITHDRAWN,
    AnnouncementKind.AWARDED: AuctionStatus.AWARDED,
}

# Which RoundResult a case's current round must carry when it is
# *discovered* (no prior `existing` row) already sitting in one of these
# terminal/failed statuses -- e.g. a crawler backlog re-sync whose first
# successful fetch for a case happens to be its 流標/停拍/撤回/拍定
# notice. Mirrors the round-result bookkeeping apply_status_transition
# does for a case we already had a row for.
_STATUS_TO_ROUND_RESULT: dict[AuctionStatus, RoundResult] = {
    AuctionStatus.FAILED: RoundResult.FAILED,
    AuctionStatus.SUSPENDED: RoundResult.SUSPENDED,
    AuctionStatus.WITHDRAWN: RoundResult.WITHDRAWN,
    AuctionStatus.AWARDED: RoundResult.AWARDED,
}


def _apply_initial_round_result(case: AuctionCase, status: AuctionStatus, winning_price_twd: int | None) -> None:
    """Set the current round's result to match a newly-discovered case's initial status.

    Only fires for FAILED/SUSPENDED/WITHDRAWN/AWARDED; ANNOUNCED/CORRECTED/
    PRICE_CHANGED/DATE_CHANGED leave the round PENDING, as normal.
    """
    result = _STATUS_TO_ROUND_RESULT.get(status)
    if result is None or case.current_round is None:
        return
    if status == AuctionStatus.AWARDED:
        if winning_price_twd is None:
            raise ValueError("a case discovered already AWARDED requires a positive winning_price_twd")
        case.current_round.winning_price_twd = winning_price_twd  # validated positive by AuctionRound
    case.current_round.result = result


@dataclass(frozen=True)
class IngestOutcome:
    case: AuctionCase
    created: bool
    status_changed: bool
    skipped_unchanged: bool


def _event_timestamp(parsed: ParsedAnnouncement, fetched_at: datetime) -> datetime:
    """The timestamp used for ``changed_at``/``first_seen_at``/``updated_at``.

    Prefers the court's own ``公告日期`` (as a midnight UTC datetime) over
    crawl wall-clock time: the crawler may fetch several announcements for
    the same case out of chronological order (e.g. a backlog re-sync, or
    simply because the source list isn't date-sorted), and
    ``apply_status_transition``'s ``changed_at`` monotonicity invariant
    must reflect when the court recorded the change, not when our process
    happened to download the page. Falls back to ``fetched_at`` only when
    the announcement has no 公告日期 at all.
    """
    if parsed.announced_date is not None:
        tzinfo = fetched_at.tzinfo or timezone.utc
        return datetime.combine(parsed.announced_date, time.min, tzinfo=tzinfo)
    return fetched_at


def ingest_auction_announcement(
    parsed: ParsedAnnouncement,
    repository: AuctionRepository,
    *,
    fetched_at: datetime,
) -> IngestOutcome:
    """Turn one parsed announcement into a case create/update.

    Ingestion glue between crawlers.auction.parser's structured output and
    the status-transition state machine above: decides whether this is a
    brand-new case, a byte-identical re-fetch (skipped), or an amendment
    to a known case, and calls ``apply_status_transition`` accordingly.
    ``fetched_at`` (actual crawl time) is used for document bookkeeping;
    the case/event timeline itself uses ``_event_timestamp`` instead --
    see its docstring.
    """
    existing = repository.get_by_case_number(parsed.court_name, parsed.case_number)

    if existing is not None:
        previous_hashes = {doc.content_hash for doc in existing.documents if doc.content_hash}
        if is_unchanged(previous_hashes, parsed):
            return IngestOutcome(case=existing, created=False, status_changed=False, skipped_unchanged=True)

    event_at = _event_timestamp(parsed, fetched_at)

    if existing is None:
        case = AuctionCase(
            court_name=parsed.court_name,
            case_number=parsed.case_number,
            first_seen_at=fetched_at,
            updated_at=event_at,
        )
        _apply_descriptive_fields(case, parsed)
        if (
            parsed.round_number is not None
            and parsed.floor_price_total_twd is not None
            and parsed.floor_unit_price_twd is not None
        ):
            case.rounds.append(
                AuctionRound(
                    round_number=parsed.round_number,
                    floor_price_total_twd=parsed.floor_price_total_twd,
                    floor_unit_price_twd=parsed.floor_unit_price_twd,
                    auction_date=parsed.auction_date,
                    deposit_twd=parsed.deposit_twd,
                )
            )
        # A case can be "discovered" via any announcement kind (e.g. the
        # crawler's first successful fetch for this case happens to be a
        # correction, failure, or award notice) -- there is no real "from"
        # status in that cold start, so this is recorded as a single
        # from_status=None event rather than a synthetic
        # ANNOUNCED-then-immediately-transition pair that would
        # misrepresent the history.
        initial_status = _KIND_TO_STATUS[parsed.kind]
        case.status = initial_status
        _apply_initial_round_result(case, initial_status, parsed.winning_price_twd)
        case.status_history.append(
            AuctionStatusHistory(
                to_status=initial_status,
                changed_at=event_at,
                from_status=None,
                round_number=case.round_number,
                note=parsed.note or "首次發現公告",
            )
        )
        for doc in extract_documents(parsed, fetched_at=fetched_at):
            case.documents.append(
                AuctionDocument(
                    doc_type=doc.doc_type,
                    url=doc.url,
                    title=doc.title,
                    fetched_at=doc.fetched_at,
                    content_hash=doc.content_hash,
                )
            )
        _assert_invariants(case)
        repository.add(case)
        return IngestOutcome(case=case, created=True, status_changed=False, skipped_unchanged=False)

    status_changed = False
    if parsed.kind == AnnouncementKind.CORRECTION:
        _apply_descriptive_fields(existing, parsed)
        apply_status_transition(existing, AuctionStatus.CORRECTED, changed_at=event_at, note=parsed.note or "更正公告")
        status_changed = True
    elif parsed.kind == AnnouncementKind.PRICE_CHANGE:
        apply_status_transition(
            existing,
            AuctionStatus.PRICE_CHANGED,
            changed_at=event_at,
            new_floor_price_total_twd=parsed.floor_price_total_twd,
            new_floor_unit_price_twd=parsed.floor_unit_price_twd,
        )
        status_changed = True
    elif parsed.kind == AnnouncementKind.DATE_CHANGE:
        apply_status_transition(
            existing, AuctionStatus.DATE_CHANGED, changed_at=event_at, new_auction_date=parsed.auction_date
        )
        status_changed = True
    elif parsed.kind == AnnouncementKind.SUSPENDED:
        apply_status_transition(existing, AuctionStatus.SUSPENDED, changed_at=event_at, note=parsed.note)
        status_changed = True
    elif parsed.kind == AnnouncementKind.WITHDRAWN:
        apply_status_transition(existing, AuctionStatus.WITHDRAWN, changed_at=event_at, note=parsed.note)
        status_changed = True
    elif parsed.kind == AnnouncementKind.AWARDED:
        apply_status_transition(
            existing,
            AuctionStatus.AWARDED,
            changed_at=event_at,
            winning_price_twd=parsed.winning_price_twd,
            note=parsed.note,
        )
        status_changed = True
    elif parsed.kind == AnnouncementKind.FAILED:
        apply_status_transition(existing, AuctionStatus.FAILED, changed_at=event_at, note=parsed.note)
        status_changed = True
        # A 流標 announcement sometimes simultaneously announces the next
        # round ("第一拍流標，訂於...進行第二拍拍賣，底價..."). Only treat
        # it as a round advance when the round data is unambiguous and
        # strictly the next round -- apply_status_transition itself
        # enforces the same "exactly current+1" rule, but checking it here
        # too lets a genuinely-just-流標 announcement (no next-round data,
        # or a malformed/gapped round number) stay in FAILED and wait for
        # a separate follow-up announcement, instead of raising and
        # aborting the whole batch over one ambiguous page.
        has_next_round_data = (
            parsed.round_number is not None
            and parsed.floor_price_total_twd is not None
            and parsed.floor_unit_price_twd is not None
            and parsed.round_number == (existing.round_number or 0) + 1
        )
        if has_next_round_data:
            next_round = AuctionRound(
                round_number=parsed.round_number,
                floor_price_total_twd=parsed.floor_price_total_twd,
                floor_unit_price_twd=parsed.floor_unit_price_twd,
                auction_date=parsed.auction_date,
                deposit_twd=parsed.deposit_twd,
            )
            apply_status_transition(existing, AuctionStatus.ANNOUNCED, changed_at=event_at, next_round=next_round)
    elif parsed.kind == AnnouncementKind.NEW and _as_aware_utc(event_at) >= _as_aware_utc(existing.updated_at):
        # Re-published NEW-kind page for an already-known case (e.g. the
        # court re-issued the same announcement) -- refresh descriptive
        # fields but don't force a status transition; NEW isn't one of
        # the three amendment kinds apply_status_transition understands.
        #
        # Explicitly stamping updated_at here (not just relying on
        # TimestampMixin's onupdate=utcnow) matters: onupdate only fires
        # when a row gets an UPDATE but updated_at itself wasn't part of
        # this flush's explicit changes -- if we mutated other columns
        # here and left updated_at untouched, the *next* unrelated
        # autoflush (e.g. from a later get_by_case_number() SELECT) would
        # silently stamp wall-clock "now" over our tracked event
        # timeline, corrupting apply_status_transition's monotonicity
        # invariant for whatever gets processed next.
        _apply_descriptive_fields(existing, parsed)
        existing.updated_at = event_at

    for doc in extract_documents(parsed, fetched_at=fetched_at):
        existing.documents.append(
            AuctionDocument(
                doc_type=doc.doc_type,
                url=doc.url,
                title=doc.title,
                fetched_at=doc.fetched_at,
                content_hash=doc.content_hash,
            )
        )
    return IngestOutcome(case=existing, created=False, status_changed=status_changed, skipped_unchanged=False)


@dataclass(frozen=True)
class RescoreResult:
    market_unit_price_twd: int
    score: AuctionScore


def rescore_case(case: AuctionCase, session: Session, *, liquidity_index: float = 0.5) -> RescoreResult | None:
    """Look up the shared ``market_prices`` row for ``case`` and cache a fresh score.

    Returns ``None`` (leaving cached scores untouched) if there's no
    round yet or no matching regional market price -- mirrors
    ``apps.services.sale_pipeline.ingest_sale_listings``'s "only score
    when we have something to compare against" behavior. The returned
    ``market_unit_price_twd`` is reused by the notification step below so
    a "new case" notification never needs a second market-price query.
    """
    if case.current_round is None:
        return None
    building_type = CASE_TYPE_LABELS.get(case.case_type, "其他")
    market = session.scalar(
        select(MarketPrice).where(
            MarketPrice.city == case.city,
            MarketPrice.district == case.district,
            MarketPrice.building_type == building_type,
        )
    )
    if market is None:
        return None
    score = score_auction_case(case, market.average_unit_price_twd, liquidity_index=liquidity_index)
    case.surface_discount_rate = score.surface_discount_rate
    case.risk_score = Decimal(risk_score_0_100(case))
    case.investment_score = Decimal(score.total)
    return RescoreResult(market_unit_price_twd=market.average_unit_price_twd, score=score)


@dataclass(frozen=True)
class IngestResult:
    processed: int = 0
    created: int = 0
    status_changed: int = 0
    skipped_unchanged: int = 0
    notifications_queued: int = 0


async def ingest_auction_announcements(
    source: AuctionAnnouncementSource,
    parser: CourtAnnouncementParser,
    session: Session,
    *,
    liquidity_index: float = 0.5,
) -> IngestResult:
    """Fetch, parse, and persist a batch of announcements, queuing a durable
    outbox row (see ``apps.services.auction_notification_outbox``) for
    every real, persisted change.

    This function does NOT deliver any Discord notification itself --
    queuing (``queue_pending_notifications``) happens inside the same
    transaction as the domain mutation it describes, ahead of this
    function's single batch ``session.commit()``, so the pending
    notification and the case data it's about are always committed
    atomically. Call ``apps.services.auction_notification_outbox.
    deliver_pending_notifications`` afterward (with a real or disabled
    notifier -- see ``apps.services.auction_notifier``) to actually send;
    that step is decoupled on purpose so a Discord outage or a crash
    between commit and delivery can never lose or roll back the already-
    persisted ingest results, and so leftover pending/failed rows from a
    previous run are always retried by whichever process next calls
    ``deliver_pending_notifications`` -- not just rows queued in this
    call.

    Only outcomes that represent a *persisted, real* change (a newly
    created case, or an actual status transition) are queued for
    notification -- never a skipped-unchanged re-fetch or a same-status
    no-op refresh -- and queuing itself dedupes on a unique
    ``delivery_key``, so re-running this against an already-ingested
    batch (e.g. the next scheduler tick re-fetching pages that haven't
    changed) can never queue the same notification twice.
    """
    repository = AuctionRepository(session)
    raw_announcements = await source.fetch()
    parsed_batch = [(raw, parser.parse(raw.raw_html, source_url=raw.source_url)) for raw in raw_announcements]
    # Apply in chronological order (by each announcement's own 公告日期),
    # not crawl/fetch order -- the status-transition state machine's
    # from/to semantics only make sense walked forward in real time, and a
    # source listing has no guaranteed order (e.g. a backlog re-sync could
    # return newest-first).
    parsed_batch.sort(key=lambda pair: _event_timestamp(pair[1], pair[0].fetched_at))

    created = 0
    status_changed = 0
    skipped = 0
    notifications_queued = 0

    for raw, parsed in parsed_batch:
        outcome = ingest_auction_announcement(parsed, repository, fetched_at=raw.fetched_at)
        if outcome.skipped_unchanged:
            skipped += 1
            continue
        rescore = rescore_case(outcome.case, session, liquidity_index=liquidity_index)
        created += int(outcome.created)
        status_changed += int(outcome.status_changed)
        if outcome.created:
            notifications_queued += queue_pending_notifications(session, outcome.case, None, rescore)
        elif outcome.status_changed:
            notifications_queued += queue_pending_notifications(
                session, outcome.case, outcome.case.status_history[-1], rescore
            )

    session.commit()

    return IngestResult(
        processed=len(raw_announcements),
        created=created,
        status_changed=status_changed,
        skipped_unchanged=skipped,
        notifications_queued=notifications_queued,
    )
