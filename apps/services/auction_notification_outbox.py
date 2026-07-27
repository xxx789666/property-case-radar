"""Durable notification outbox for the auction pipeline.

``apps.services.auction_pipeline.ingest_auction_announcements`` queues
one ``NotificationLog`` row per (case/event, channel-kind) destination
via ``queue_pending_notifications``, called INSIDE the same transaction
as the domain mutation it describes -- so a pending notification and the
case data it's about are always committed atomically (see that
function's docstring for why ``session.flush()`` is required there).

Actual Discord delivery happens strictly after that commit, in
``deliver_pending_notifications``, which is deliberately decoupled from
any particular ingestion run: it queries every pending/retryable row
straight from the database (never a caller-supplied in-memory list), so
a process crash or Discord outage between commit and delivery can never
lose a notification -- the next call to ``deliver_pending_notifications``
(e.g. the next scheduler tick) picks up exactly where the last one left
off. Delivery of a given ``delivery_key`` is idempotent: the row's
``status`` moves pending -> delivered on success (excluded from future
delivery queries) or pending -> failed on failure (retried up to
``MAX_NOTIFY_ATTEMPTS``, then left ``failed`` and no longer retried --
bounded retry, not an infinite loop against a permanently-broken
channel). Re-ingesting an already-processed announcement can never queue
a second row for the same logical notification, because
``queue_pending_notifications`` dedupes on the same unique
``delivery_key`` the DB itself enforces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.auction import AuctionCase, AuctionStatusHistory
from database.models.base import utcnow
from database.models.common import NotificationLog
from notifications.auction_notification import (
    AuctionNotificationRouter,
    channels_for_new_case,
    channels_for_status_event,
)

logger = logging.getLogger(__name__)

MAX_NOTIFY_ATTEMPTS = 5


def _delivery_key_for_new_case(case_id: int, kind: str) -> str:
    return f"auction:new:{case_id}:{kind}"


def _delivery_key_for_status_event(event_id: int, kind: str) -> str:
    return f"auction:status:{event_id}:{kind}"


def queue_pending_notifications(
    session: Session,
    case: AuctionCase,
    event: AuctionStatusHistory | None,
    rescore,
    *,
    high_score_threshold: int = 80,
    upcoming_within_days: int = 7,
) -> int:
    """Create pending ``NotificationLog`` rows for every channel-kind this
    case/event should be published to.

    Must be called BEFORE the caller's ``session.commit()`` (see
    ``apps.services.auction_pipeline.ingest_auction_announcements``, which
    calls this immediately after rescoring, ahead of its single batch
    commit) so the outbox rows land in the same transaction as the domain
    change they describe. ``rescore`` is an
    ``apps.services.auction_pipeline.RescoreResult | None`` -- typed
    loosely here (not imported) to avoid a circular import, since
    ``auction_pipeline`` imports this module.

    Calls ``session.flush()`` first to guarantee ``case.id`` (and
    ``event.id``, if given) are populated -- both are required to build a
    ``delivery_key`` -- even if the caller hasn't flushed yet.

    Returns how many *new* rows were queued (0 if every relevant
    ``delivery_key`` already existed -- safe to call repeatedly for the
    same logical event).
    """
    session.flush()
    if event is None:
        if rescore is None:
            return 0
        kinds = channels_for_new_case(
            case, rescore.score, high_score_threshold=high_score_threshold, upcoming_within_days=upcoming_within_days
        )
        status_history_id = None

        def key_for(kind: str) -> str:
            return _delivery_key_for_new_case(case.id, kind)
    else:
        kinds = channels_for_status_event(case, event, upcoming_within_days=upcoming_within_days)
        status_history_id = event.id

        def key_for(kind: str) -> str:
            return _delivery_key_for_status_event(event.id, kind)

    queued = 0
    for kind in kinds:
        key = key_for(kind)
        already_queued = session.scalar(select(NotificationLog.id).where(NotificationLog.delivery_key == key))
        if already_queued is not None:
            continue
        session.add(
            NotificationLog(
                auction_case_id=case.id,
                status_history_id=status_history_id,
                kind=kind,
                delivery_key=key,
            )
        )
        queued += 1
    return queued


@dataclass(frozen=True)
class DeliveryReport:
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    exhausted: int = 0  # hit MAX_NOTIFY_ATTEMPTS this call and will not be retried again


def _fail_row(row: NotificationLog, error: str) -> None:
    row.attempt_count += 1
    row.status = "failed"
    row.last_error = error[:2000]


async def deliver_pending_notifications(
    session: Session,
    router: AuctionNotificationRouter,
    *,
    max_attempts: int = MAX_NOTIFY_ATTEMPTS,
    liquidity_index: float = 0.5,
    limit: int | None = None,
) -> DeliveryReport:
    """Drain the outbox: attempt every pending/retryable ``NotificationLog`` row.

    Queries the database fresh on every call rather than trusting any
    in-memory queue from the ingestion run that created the rows -- this
    is what makes delivery resilient to a process restart/crash between a
    previous commit and its delivery attempt, and lets a completely
    separate process (or a later scheduler tick) finish delivering
    whatever an earlier run only got partway through. Commits after each
    row so a crash mid-batch leaves already-delivered rows durably marked
    as such (not re-sent on the next call).
    """
    # Local imports: apps.services.auction_pipeline imports
    # queue_pending_notifications from this module at module load time,
    # so importing it back at module scope here would be a circular
    # import. Both modules are fully loaded by the time this function is
    # actually called, so a deferred (function-body) import is safe.
    from apps.services.auction_pipeline import rescore_case
    from database.repositories.auction import AuctionRepository

    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    statement = (
        select(NotificationLog)
        .where(NotificationLog.status.in_(("pending", "failed")))
        .where(NotificationLog.attempt_count < max_attempts)
        .where(NotificationLog.auction_case_id.is_not(None))
        .order_by(NotificationLog.id)
    )
    if limit is not None:
        statement = statement.limit(limit)
    rows = list(session.scalars(statement))

    repo = AuctionRepository(session)
    attempted = delivered = failed = exhausted = 0

    for row in rows:
        attempted += 1

        case = repo.get(row.auction_case_id)
        if case is None:
            _fail_row(row, "referenced auction_case no longer exists")
            session.commit()
            failed += 1
            if row.attempt_count >= max_attempts:
                exhausted += 1
            continue

        if row.status_history_id is not None:
            event = next((e for e in case.status_history if e.id == row.status_history_id), None)
            if event is None:
                _fail_row(row, "referenced status_history event no longer exists")
                session.commit()
                failed += 1
                if row.attempt_count >= max_attempts:
                    exhausted += 1
                continue
            outcome = await router.send_status_event(row.kind, case, event)
        else:
            rescore = rescore_case(case, session, liquidity_index=liquidity_index)
            if rescore is None:
                _fail_row(row, "no regional market price available yet")
                session.commit()
                failed += 1
                if row.attempt_count >= max_attempts:
                    exhausted += 1
                continue
            outcome = await router.send_new_case(row.kind, case, rescore.market_unit_price_twd, rescore.score)

        row.attempt_count += 1
        if outcome.success:
            row.status = "delivered"
            row.delivered_at = utcnow()
            row.last_error = None
            delivered += 1
        else:
            row.status = "failed"
            row.last_error = (outcome.error or "unknown error")[:2000]
            failed += 1
            if row.attempt_count >= max_attempts:
                exhausted += 1
                logger.error(
                    "auction notification %s exhausted retries after %s attempts: %s",
                    row.delivery_key,
                    row.attempt_count,
                    row.last_error,
                )
        session.commit()

    return DeliveryReport(attempted=attempted, delivered=delivered, failed=failed, exhausted=exhausted)
