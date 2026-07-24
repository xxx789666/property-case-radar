"""Coverage for apps.services.auction_notification_outbox -- Fix (3):
a durable NotificationLog outbox that queues pending rows inside the
ingestion transaction, delivers strictly afterward from a fresh DB
query (not an in-memory list), dedupes successes, and bounds retries
on repeated failure.

No live Discord connection anywhere in this file: every
AuctionNotificationRouter channel is an AsyncMock.
"""

from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.services.auction_notification_outbox import (
    MAX_NOTIFY_ATTEMPTS,
    deliver_pending_notifications,
    queue_pending_notifications,
)
from apps.services.auction_pipeline import rescore_case
from database.models.auction import AuctionCase, AuctionRound
from database.models.common import MarketPrice, NotificationLog
from notifications.auction_notification import AuctionNotificationRouter


def _router(**overrides) -> AuctionNotificationRouter:
    defaults = dict(
        new_channel=AsyncMock(),
        upcoming_channel=AsyncMock(),
        round_channel=AsyncMock(),
        high_score_channel=AsyncMock(),
        suspended_channel=AsyncMock(),
    )
    defaults.update(overrides)
    return AuctionNotificationRouter(**defaults)


def _seed_case(session, *, round_number: int = 2) -> AuctionCase:
    case = AuctionCase(
        court_name="桃園地方法院",
        case_number=f"outbox-test-{round_number}",
        city="桃園市",
        district="中壢區",
        rounds=[
            AuctionRound(
                round_number=round_number,
                floor_price_total_twd=9_800_000,
                floor_unit_price_twd=232_000,
                auction_date=date(2026, 8, 18),
            )
        ],
    )
    session.add(case)
    session.add(
        MarketPrice(
            city="桃園市", district="中壢區", building_type="其他", average_unit_price_twd=358_000, transaction_count=40
        )
    )
    session.commit()
    return case


def test_queue_pending_notifications_dedupes_across_repeat_calls(session_factory) -> None:
    with session_factory() as session:
        case = _seed_case(session)
        rescore = rescore_case(case, session)

        first = queue_pending_notifications(session, case, None, rescore)
        session.commit()
        assert first > 0

        second = queue_pending_notifications(session, case, None, rescore)
        session.commit()
        assert second == 0

        rows = session.scalars(select(NotificationLog)).all()
        assert len(rows) == first  # no duplicate rows from the repeat call


@pytest.mark.asyncio
async def test_one_failing_channel_does_not_block_delivery_to_sibling_channels(session_factory) -> None:
    with session_factory() as session:
        # round_number=2 routes to both "new" and "round".
        case = _seed_case(session, round_number=2)
        rescore = rescore_case(case, session)
        queue_pending_notifications(session, case, None, rescore)
        session.commit()

        new_channel = AsyncMock()
        new_channel.send = AsyncMock(side_effect=RuntimeError("new channel unavailable"))
        router = _router(new_channel=new_channel)

        report = await deliver_pending_notifications(session, router)

        assert report.failed == 1
        assert report.delivered == 1

        rows = {row.kind: row for row in session.scalars(select(NotificationLog)).all()}
        assert rows["new"].status == "failed"
        assert "new channel unavailable" in rows["new"].last_error
        assert rows["round"].status == "delivered"
        assert rows["round"].delivered_at is not None
        router.round_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_bounded_retry_stops_after_max_attempts_and_marks_row_failed(session_factory) -> None:
    with session_factory() as session:
        case = _seed_case(session, round_number=1)  # routes to "new" only
        rescore = rescore_case(case, session)
        queue_pending_notifications(session, case, None, rescore)
        session.commit()

        always_fails = AsyncMock()
        always_fails.send = AsyncMock(side_effect=RuntimeError("discord down"))
        router = _router(new_channel=always_fails)

        max_attempts = 3
        last_report = None
        for _ in range(max_attempts):
            last_report = await deliver_pending_notifications(session, router, max_attempts=max_attempts)
            assert last_report.attempted == 1

        assert last_report.failed == 1
        assert last_report.exhausted == 1

        row = session.scalars(select(NotificationLog)).one()
        assert row.status == "failed"
        assert row.attempt_count == max_attempts

        # Further calls no longer pick up the exhausted row at all.
        quiet_report = await deliver_pending_notifications(session, router, max_attempts=max_attempts)
        assert quiet_report.attempted == 0
        assert always_fails.send.await_count == max_attempts


@pytest.mark.asyncio
async def test_delivery_recovers_a_row_left_pending_by_a_prior_crashed_process(session_factory) -> None:
    """Simulate a crash between commit-of-pending-row and delivery: insert
    a pending NotificationLog directly (bypassing queue_pending_notifications,
    as if an earlier process had already queued it and then died before
    ever calling deliver_pending_notifications), then confirm a completely
    fresh call still finds and delivers it -- proving delivery is driven by
    a DB query, never a caller-held in-memory queue.
    """
    with session_factory() as session:
        case = _seed_case(session, round_number=1)
        session.add(
            NotificationLog(
                auction_case_id=case.id,
                status_history_id=None,
                kind="new",
                delivery_key=f"auction:new:{case.id}:new",
            )
        )
        session.commit()

        router = _router()
        report = await deliver_pending_notifications(session, router)

        assert report.attempted == 1
        assert report.delivered == 1
        row = session.scalars(select(NotificationLog)).one()
        assert row.status == "delivered"
        router.new_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_successfully_delivered_rows_are_never_resent(session_factory) -> None:
    with session_factory() as session:
        case = _seed_case(session, round_number=1)
        rescore = rescore_case(case, session)
        queue_pending_notifications(session, case, None, rescore)
        session.commit()

        router = _router()
        first_report = await deliver_pending_notifications(session, router)
        assert first_report.delivered == 1
        router.new_channel.send.assert_awaited_once()

        second_report = await deliver_pending_notifications(session, router)
        assert second_report.attempted == 0
        assert second_report.delivered == 0
        router.new_channel.send.assert_awaited_once()  # still just the one call


def test_max_notify_attempts_is_a_small_bounded_constant() -> None:
    # Sanity check that the module's default retry bound is intentionally
    # small/finite, not e.g. left as an unbounded loop.
    assert 1 <= MAX_NOTIFY_ATTEMPTS <= 10
