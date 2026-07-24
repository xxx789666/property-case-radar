import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from apps.config import Settings, get_settings
from apps.services.auction_notification_outbox import deliver_pending_notifications
from apps.services.auction_notifier import live_auction_notifier
from apps.services.auction_pipeline import ingest_auction_announcements
from crawlers.auction.court_crawler import AuctionAnnouncementSource
from crawlers.auction.parser import CourtAnnouncementParser
from crawlers.transaction.moi_open_data import MoiActualPriceSource, sync_market_prices
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


def build_production_scheduler(
    market_job: Callable[[], None],
    market_interval_hours: int,
) -> BlockingScheduler:
    """Build the unattended production scheduler.

    Only the official MOI current-batch job is registered.  Live auction
    ingestion intentionally remains absent while every approved official
    auction source requires either robots-prohibited access (Judicial Yuan)
    or a CAPTCHA (MOJ Administrative Enforcement Agency).
    """

    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        market_job,
        "interval",
        hours=market_interval_hours,
        id="market-price-sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    return scheduler


def make_market_sync_job(
    factory: sessionmaker[Session],
    source: MoiActualPriceSource,
) -> Callable[[], None]:
    def run_market_sync() -> None:
        with factory() as session:
            result = asyncio.run(sync_market_prices(source, session))
            logger.info(
                "official MOI market sync finished: fetched=%s skipped=%s groups=%s "
                "created=%s updated=%s unchanged=%s downloaded=%s",
                result.fetched_records,
                result.skipped_rows,
                result.groups,
                result.created,
                result.updated,
                result.unchanged,
                result.downloaded,
            )

    return run_market_sync


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
    """Run the production scheduler.

    Fixture sale/auction composition remains available through the helpers
    above for deterministic tests, but the long-running entrypoint uses only
    the licensed official MOI download.  It does not crawl 591/Sinyi and it
    does not attempt CAPTCHA/robots-prohibited auction sources.
    """

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    market_source = MoiActualPriceSource(cache_dir=settings.moi_cache_dir)
    scheduler = build_production_scheduler(
        make_market_sync_job(factory, market_source),
        settings.market_sync_interval_hours,
    )
    logger.info(
        "scheduler started; market source=official MOI current sales Open Data; "
        "live auction source disabled (robots/CAPTCHA controls)"
    )
    scheduler.start()


if __name__ == "__main__":
    main()
