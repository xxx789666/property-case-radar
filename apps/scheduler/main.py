import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from apps.config import Settings, get_settings
from apps.services.auction_notification_outbox import deliver_pending_notifications
from apps.services.auction_notifier import live_auction_notifier
from apps.services.auction_pipeline import ingest_auction_announcements
from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.auction.court_crawler import AuctionAnnouncementSource, FixtureAuctionAnnouncementSource
from crawlers.auction.parser import CourtAnnouncementParser
from crawlers.sale import FixtureSaleCrawler
from database.models import Base
from database.session import create_db_engine, create_session_factory
from notifications.auction_notification import AuctionNotificationRouter

logger = logging.getLogger(__name__)


def build_scheduler(
    sale_job: Callable[[], None],
    sale_interval_minutes: int,
    *,
    auction_job: Callable[[], None] | None = None,
    auction_interval_hours: int | None = None,
) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        sale_job,
        "interval",
        minutes=sale_interval_minutes,
        id="sale-crawler",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if auction_job is not None:
        scheduler.add_job(
            auction_job,
            "interval",
            hours=auction_interval_hours or 8,
            id="auction-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    return scheduler


def make_auction_job(
    factory: sessionmaker[Session],
    source: AuctionAnnouncementSource,
    parser: CourtAnnouncementParser,
    *,
    liquidity_index: float = 0.5,
    notifier: AuctionNotificationRouter | None = None,
) -> Callable[[], None]:
    """Test/manual-composition auction job: ingest+queue, then (if
    ``notifier`` is given) immediately attempt delivery of the outbox
    with it.

    Takes a directly-injected router rather than building one itself --
    handy for tests (a fake/mock router) or a future combined process
    that already holds a live router open across ticks. See
    ``make_live_auction_job`` for the production entrypoint that builds a
    real-or-disabled notifier itself, fresh, every tick.
    """

    async def run_auction_cycle() -> None:
        with factory() as session:
            result = await ingest_auction_announcements(source, parser, session, liquidity_index=liquidity_index)
            logger.info("auction crawl finished: %s", result)
            if notifier is not None:
                report = await deliver_pending_notifications(session, notifier, liquidity_index=liquidity_index)
                logger.info("auction notification delivery finished: %s", report)

    def run_auction_job() -> None:
        asyncio.run(run_auction_cycle())

    return run_auction_job


def make_live_auction_job(
    factory: sessionmaker[Session],
    source: AuctionAnnouncementSource,
    parser: CourtAnnouncementParser,
    settings: Settings,
    *,
    liquidity_index: float = 0.5,
) -> Callable[[], None]:
    """Production auction job: ingest+queue, then attempt delivery through
    a real (or explicitly logged-and-disabled) Discord notifier built
    fresh each tick via ``apps.services.auction_notifier.live_auction_notifier``.

    This is the entrypoint ``main()`` actually uses. Unlike
    ``make_auction_job``, it never hardcodes ``notifier=None`` --
    whether live delivery happens each tick depends entirely on whether
    ``settings.discord_token`` is configured and valid at that moment,
    checked and logged explicitly every time (see
    ``live_auction_notifier``'s docstring). A missing/invalid token never
    stops ingestion -- announcements are still fetched, persisted, and
    queued in the outbox regardless, waiting for a future run with a
    working token to deliver them.
    """

    async def run_auction_cycle() -> None:
        with factory() as session:
            result = await ingest_auction_announcements(source, parser, session, liquidity_index=liquidity_index)
            logger.info("auction crawl finished: %s", result)
            async with live_auction_notifier(settings) as notifier:
                if notifier is not None:
                    report = await deliver_pending_notifications(session, notifier, liquidity_index=liquidity_index)
                    logger.info("auction notification delivery finished: %s", report)

    def run_auction_job() -> None:
        asyncio.run(run_auction_cycle())

    return run_auction_job


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    sale_fixture = Path("tests/fixtures/sale_listings.json")
    sale_crawler = FixtureSaleCrawler(sale_fixture)

    def run_sale_job() -> None:
        with factory() as session:
            result = asyncio.run(ingest_sale_listings(sale_crawler, session))
            logger.info("sale crawl finished: %s", result)

    auction_source = FixtureAuctionAnnouncementSource(Path("crawlers/auction/fixtures"))
    auction_parser = CourtAnnouncementParser()
    run_auction_job = make_live_auction_job(factory, auction_source, auction_parser, settings)

    scheduler = build_scheduler(
        run_sale_job,
        settings.sale_crawl_interval_minutes,
        auction_job=run_auction_job,
        auction_interval_hours=settings.auction_crawl_interval_hours,
    )
    logger.info("scheduler started; sale source=offline fixture, auction source=offline fixture")
    scheduler.start()


if __name__ == "__main__":
    main()
