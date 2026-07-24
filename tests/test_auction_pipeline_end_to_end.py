"""End-to-end coverage: crawlers.auction.parser -> apps.services.auction_pipeline
-> database -> notifications.auction_notification.AuctionNotificationRouter.

Exercises the full fixture-backed pipeline exactly as
apps/scheduler/main.py's ``make_auction_job`` wires it, including
notification delivery, delivery-failure handling, terminal-status
scheduling exclusion, and the FAILED/SUSPENDED/WITHDRAWN/AWARDED
ingestion paths added in this round. No live network or Discord
connection anywhere in this file -- ``AuctionNotificationRouter`` is
always constructed with ``unittest.mock.AsyncMock`` channels.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from apps.scheduler.main import make_auction_job
from apps.services.auction_notification_outbox import deliver_pending_notifications
from apps.services.auction_pipeline import ingest_auction_announcement, ingest_auction_announcements
from crawlers.auction.court_crawler import FixtureAuctionAnnouncementSource
from crawlers.auction.parser import CourtAnnouncementParser
from database.models.auction import AuctionStatus, RoundResult
from database.models.common import MarketPrice, NotificationLog
from database.repositories.auction import AuctionRepository


def _make_router(**overrides):
    defaults = dict(
        new_channel=AsyncMock(),
        upcoming_channel=AsyncMock(),
        round_channel=AsyncMock(),
        high_score_channel=AsyncMock(),
        suspended_channel=AsyncMock(),
    )
    defaults.update(overrides)
    from notifications.auction_notification import AuctionNotificationRouter

    return AuctionNotificationRouter(**defaults)


@pytest.mark.asyncio
async def test_full_fixture_set_reaches_expected_terminal_states(session_factory) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        result = await ingest_auction_announcements(source, parser, session)
        assert result.created == 4

        repo = AuctionRepository(session)

        active_case = repo.get_by_case_number("桃園地方法院", "115年度司執字第XXXX號".replace("XXXX", "12345"))
        assert active_case.status == AuctionStatus.DATE_CHANGED

        awarded_case = repo.get_by_case_number("台北地方法院", "115年度司執字第67890號")
        assert awarded_case.status == AuctionStatus.AWARDED
        assert awarded_case.round_number == 2
        assert awarded_case.winning_price_twd == 12_500_000
        assert [r.result for r in awarded_case.rounds] == [RoundResult.FAILED, RoundResult.AWARDED]

        suspended_case = repo.get_by_case_number("台中地方法院", "115年度司執字第55555號")
        assert suspended_case.status == AuctionStatus.SUSPENDED
        assert suspended_case.current_round.result == RoundResult.SUSPENDED

        withdrawn_case = repo.get_by_case_number("高雄地方法院", "115年度司執字第44444號")
        assert withdrawn_case.status == AuctionStatus.WITHDRAWN
        assert withdrawn_case.current_round.result == RoundResult.WITHDRAWN


@pytest.mark.asyncio
async def test_terminal_cases_are_excluded_from_upcoming_schedule(session_factory) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        await ingest_auction_announcements(source, parser, session)

        upcoming = AuctionRepository(session).upcoming(within_days=3650, today=date(2026, 7, 24))
        case_numbers = {c.case_number for c in upcoming}
        assert case_numbers == {"115年度司執字第12345號"}  # only the still-ACTIVE case


@pytest.mark.asyncio
async def test_notifier_receives_new_case_and_status_event_calls(session_factory) -> None:
    with session_factory() as session:
        session.add(
            MarketPrice(
                city="桃園市", district="中壢區", building_type="住宅", average_unit_price_twd=358_000, transaction_count=40
            )
        )
        session.commit()
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        router = _make_router()

        result = await ingest_auction_announcements(source, parser, session)
        assert result.notifications_queued > 0

        report = await deliver_pending_notifications(session, router)

        assert report.failed == 0
        # Only the 桃園 case has a seeded MarketPrice row, so its "new
        # case" event is the only one of the 4 created cases that gets
        # queued at all (queue_pending_notifications skips queuing
        # entirely when rescore_case can't find a market price) -- the
        # other 3 creations queue nothing. All status-change events queue
        # and deliver regardless, since build_status_update_notification
        # never needs a score.
        assert report.delivered == result.notifications_queued
        router.new_channel.send.assert_awaited()


@pytest.mark.asyncio
async def test_notifier_failure_is_logged_not_raised_and_does_not_lose_committed_state(session_factory, caplog) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        router = _make_router()
        router.new_channel.send = AsyncMock(side_effect=RuntimeError("discord is down"))

        result = await ingest_auction_announcements(source, parser, session)
        assert result.created == 4  # DB writes happened regardless of delivery

        # State is durably committed before any delivery attempt is even
        # made -- ingestion's own commit() already ran.
        repo = AuctionRepository(session)
        case = repo.get_by_case_number("台北地方法院", "115年度司執字第67890號")
        assert case is not None
        assert case.status == AuctionStatus.AWARDED

        # No exception propagated even though every "new" send raises.
        report = await deliver_pending_notifications(session, router)
        assert report.failed >= 1

        # Failed rows stay "failed" (retryable), not lost.
        failed_rows = session.query(NotificationLog).filter(NotificationLog.status == "failed").all()
        assert len(failed_rows) == report.failed
        assert all(row.attempt_count == 1 for row in failed_rows)
        assert all(row.last_error and "discord is down" in row.last_error for row in failed_rows)


@pytest.mark.asyncio
async def test_rerunning_ingest_does_not_resend_notifications(session_factory) -> None:
    with session_factory() as session:
        source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
        parser = CourtAnnouncementParser()
        router = _make_router()

        first = await ingest_auction_announcements(source, parser, session)
        assert first.notifications_queued > 0
        first_report = await deliver_pending_notifications(session, router)
        assert first_report.delivered > 0
        call_count_after_first = router.new_channel.send.await_count

        # Re-running ingest against the same unchanged fixtures queues
        # nothing new (delivery_key dedupe), and re-running delivery sends
        # nothing further for rows already marked "delivered".
        second = await ingest_auction_announcements(source, parser, session)
        assert second.notifications_queued == 0
        second_report = await deliver_pending_notifications(session, router)
        assert second_report.attempted == 0
        assert second_report.delivered == 0
        assert router.new_channel.send.await_count == call_count_after_first  # unchanged


def test_scheduler_make_auction_job_wires_notifier_end_to_end(session_factory) -> None:
    # Deliberately NOT an async test: make_auction_job returns a plain
    # synchronous callable (APScheduler's job interface), which manages
    # its own event loop via asyncio.run() internally -- calling it from
    # inside a pytest-asyncio test would raise "asyncio.run() cannot be
    # called from a running event loop".
    source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
    parser = CourtAnnouncementParser()
    router = _make_router()

    job = make_auction_job(session_factory, source, parser, notifier=router)
    job()  # synchronous APScheduler-style callable; wraps the asyncio.run internally

    router.new_channel.send.assert_awaited()
    with session_factory() as session:
        case = AuctionRepository(session).get_by_case_number("台北地方法院", "115年度司執字第67890號")
        assert case.status == AuctionStatus.AWARDED


# --- FAILED + round-advance / gap-rejection at the ingestion level -------

_FAILED_WITH_GAP_HTML = """
<article class="court-announcement" data-kind="failed">
  <dl>
    <dt>法院</dt><dd>測試法院</dd>
    <dt>案號</dt><dd>gap-case-001</dd>
    <dt>拍次</dt><dd>4</dd>
    <dt>底價</dt><dd>1000000</dd>
    <dt>底價單價</dt><dd>10000</dd>
    <dt>公告日期</dt><dd>2026-08-02</dd>
  </dl>
</article>
"""


@pytest.mark.asyncio
async def test_failed_announcement_with_round_gap_does_not_auto_advance(session_factory) -> None:
    with session_factory() as session:
        repo = AuctionRepository(session)
        parser = CourtAnnouncementParser()

        # Seed an existing case at round 2 via the "new" fixture-style flow.
        new_html = """
        <article class="court-announcement" data-kind="new">
          <dl>
            <dt>法院</dt><dd>測試法院</dd>
            <dt>案號</dt><dd>gap-case-001</dd>
            <dt>拍次</dt><dd>2</dd>
            <dt>底價</dt><dd>2000000</dd>
            <dt>底價單價</dt><dd>20000</dd>
            <dt>公告日期</dt><dd>2026-07-01</dd>
          </dl>
        </article>
        """
        outcome = ingest_auction_announcement(parser.parse(new_html), repo, fetched_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert outcome.created

        failed_gap = parser.parse(_FAILED_WITH_GAP_HTML)  # round_number=4, but current is 2 (needs 3)
        outcome2 = ingest_auction_announcement(
            failed_gap, repo, fetched_at=datetime(2026, 8, 2, tzinfo=timezone.utc)
        )
        assert outcome2.case.status == AuctionStatus.FAILED
        assert outcome2.case.round_number == 2  # unchanged -- no bogus round-4 jump
        assert len(outcome2.case.rounds) == 1


# --- AWARDED without winning price ---------------------------------------

_DISCOVERED_AWARDED_NO_PRICE_HTML = """
<article class="court-announcement" data-kind="awarded">
  <dl>
    <dt>法院</dt><dd>測試法院</dd>
    <dt>案號</dt><dd>malformed-awarded-001</dd>
    <dt>拍次</dt><dd>1</dd>
    <dt>底價</dt><dd>1000000</dd>
    <dt>底價單價</dt><dd>10000</dd>
    <dt>公告日期</dt><dd>2026-08-01</dd>
  </dl>
</article>
"""


@pytest.mark.asyncio
async def test_discovering_a_case_awarded_without_winning_price_raises(session_factory) -> None:
    with session_factory() as session:
        repo = AuctionRepository(session)
        parser = CourtAnnouncementParser()
        parsed = parser.parse(_DISCOVERED_AWARDED_NO_PRICE_HTML)
        with pytest.raises(ValueError, match="winning_price_twd"):
            ingest_auction_announcement(parsed, repo, fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_existing_case_awarded_kind_without_winning_price_raises(session_factory) -> None:
    with session_factory() as session:
        repo = AuctionRepository(session)
        parser = CourtAnnouncementParser()
        new_html = """
        <article class="court-announcement" data-kind="new">
          <dl>
            <dt>法院</dt><dd>測試法院</dd>
            <dt>案號</dt><dd>malformed-awarded-002</dd>
            <dt>拍次</dt><dd>1</dd>
            <dt>底價</dt><dd>1000000</dd>
            <dt>底價單價</dt><dd>10000</dd>
            <dt>公告日期</dt><dd>2026-07-01</dd>
          </dl>
        </article>
        """
        ingest_auction_announcement(parser.parse(new_html), repo, fetched_at=datetime(2026, 7, 1, tzinfo=timezone.utc))

        awarded_no_price_html = """
        <article class="court-announcement" data-kind="awarded">
          <dl>
            <dt>法院</dt><dd>測試法院</dd>
            <dt>案號</dt><dd>malformed-awarded-002</dd>
            <dt>公告日期</dt><dd>2026-08-01</dd>
          </dl>
        </article>
        """
        with pytest.raises(ValueError, match="positive winning_price_twd"):
            ingest_auction_announcement(
                parser.parse(awarded_no_price_html), repo, fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
            )
