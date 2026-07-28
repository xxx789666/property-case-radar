from unittest.mock import AsyncMock, patch

import discord

from apps.config import Settings
from apps.scheduler.main import build_scheduler, make_live_auction_job
from crawlers.auction.court_crawler import (
    AuctionAnnouncementSource,
    FixtureAuctionAnnouncementSource,
)
from crawlers.auction.parser import CourtAnnouncementParser
from database.models.common import MarketPrice, NotificationLog

_FAKE_TOKEN = "fake-token-for-tests-do-not-use.abcdef.ghijklmnop"  # nosec: not a real credential


def test_scheduler_registers_sale_job_only_by_default() -> None:
    scheduler = build_scheduler(lambda: None, 90)
    assert scheduler.get_job("sale-crawler") is not None
    assert scheduler.get_job("auction-crawler") is None


def test_scheduler_registers_both_jobs_when_auction_job_given() -> None:
    scheduler = build_scheduler(lambda: None, 90, auction_job=lambda: None, auction_interval_hours=8)
    sale_job = scheduler.get_job("sale-crawler")
    auction_job = scheduler.get_job("auction-crawler")
    assert sale_job is not None
    assert auction_job is not None
    assert sale_job.max_instances == 1
    assert auction_job.max_instances == 1


def test_auction_job_defaults_to_daily_interval_when_unset() -> None:
    scheduler = build_scheduler(lambda: None, 90, auction_job=lambda: None)
    job = scheduler.get_job("auction-crawler")
    assert job.trigger.interval.total_seconds() == 24 * 3600


def test_make_live_auction_job_delivers_through_the_full_production_wiring(session_factory) -> None:
    # Production composition (Fix 1): main() wires make_live_auction_job,
    # which must build a real, working AuctionNotificationRouter from a
    # configured token -- not a hardcoded notifier=None -- and drive a
    # full ingest -> queue -> deliver cycle through it. discord.Client's
    # login/close/fetch_channel are the only things mocked; everything
    # else (ingestion, outbox queuing, scoring, delivery) runs for real.
    with session_factory() as session:
        session.add(
            MarketPrice(
                city="桃園市", district="中壢區", building_type="其他", average_unit_price_twd=358_000, transaction_count=40
            )
        )
        session.commit()

    settings = Settings(discord_token=_FAKE_TOKEN)
    source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
    parser = CourtAnnouncementParser()

    fetched_channel_ids: list[int] = []

    async def fake_fetch_channel(channel_id, /):
        fetched_channel_ids.append(channel_id)
        return AsyncMock()

    with (
        patch.object(discord.Client, "login", AsyncMock()) as login_mock,
        patch.object(discord.Client, "close", AsyncMock()) as close_mock,
        patch.object(discord.Client, "fetch_channel", AsyncMock(side_effect=fake_fetch_channel)),
    ):
        run_auction_job = make_live_auction_job(session_factory, source, parser, settings)
        run_auction_job()  # synchronous APScheduler-style callable

    login_mock.assert_awaited_once_with(_FAKE_TOKEN)
    close_mock.assert_awaited_once()
    assert fetched_channel_ids  # at least one destination channel was actually contacted

    with session_factory() as session:
        delivered = session.query(NotificationLog).filter(NotificationLog.status == "delivered").count()
        assert delivered > 0


def test_failed_counties_retry_three_times_at_fifteen_minute_intervals(
    session_factory,
) -> None:
    class AlwaysFailingCountySource(AuctionAnnouncementSource):
        def __init__(self) -> None:
            super().__init__()
            self.last_failed_counties = ("彰化縣",)
            self.retry_calls = 0

        async def fetch(self):
            return []

        async def retry_failed_counties(self, counties):
            assert counties == ("彰化縣",)
            self.retry_calls += 1
            self.last_failed_counties = ("彰化縣",)
            return []

    source = AlwaysFailingCountySource()
    settings = Settings(
        discord_token=None,
        auction_failed_retry_delay_minutes=15,
        auction_failed_retry_rounds=3,
    )

    with patch("apps.scheduler.main.asyncio.sleep", AsyncMock()) as sleep_mock:
        run_auction_job = make_live_auction_job(
            session_factory,
            source,
            CourtAnnouncementParser(),
            settings,
        )
        run_auction_job()

    assert source.retry_calls == 3
    assert sleep_mock.await_count == 3
    assert [call.args[0] for call in sleep_mock.await_args_list] == [900, 900, 900]
