import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from apps.config import Settings, get_settings
from apps.services.auction_daily_summary import deliver_daily_auction_summary
from apps.services.auction_notification_outbox import deliver_pending_notifications
from apps.services.auction_notifier import live_auction_notifier
from apps.services.auction_pipeline import ingest_auction_announcements
from apps.services.sale_notifier import live_sale_notifier
from apps.services.sale_pipeline import ingest_sale_listings
from apps.services.sale_daily_summary import deliver_daily_sale_summary
from apps.services.sale_status_lifecycle import reconcile_stale_sale_listings
from crawlers.auction.court_crawler import AuctionAnnouncementSource
from crawlers.auction.captured_source import CapturedAuctionAnnouncementSource
from crawlers.auction.moj_detail_parser import MojEstateDetailParser
from crawlers.auction.parser import CourtAnnouncementParser
from crawlers.transaction.moi_open_data import MoiActualPriceSource, sync_market_prices
from crawlers.sale.captured_source import CapturedSaleCrawler
from crawlers.sale.captured_status import (
    CapturedSaleStatusVerifier,
    SaleStatusVerificationError,
)
from database.models.sale import Property
from database.session import create_db_engine, create_session_factory
from notifications.auction_notification import AuctionNotificationRouter
from sqlalchemy import func, select

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
            hours=auction_interval_hours or 24,
            id="auction-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    return scheduler


def build_production_scheduler(
    market_job: Callable[[], None],
    market_interval_hours: int,
    *,
    sale_job: Callable[[], None] | None = None,
    sale_interval_minutes: int = 1440,
    auction_job: Callable[[], None] | None = None,
    auction_interval_hours: int = 24,
) -> BlockingScheduler:
    """Build the official-data production scheduler."""

    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    if sale_job is not None:
        scheduler.add_job(
            sale_job,
            "interval",
            minutes=sale_interval_minutes,
            id="sale-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            # The operator seeds the first baseline before restarting the
            # scheduler.  Waiting one full interval also avoids launching
            # two Playwright captures together at process startup.
            next_run_time=datetime.now(timezone.utc)
            + timedelta(minutes=sale_interval_minutes),
        )
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
    if auction_job is not None:
        scheduler.add_job(
            auction_job,
            "interval",
            hours=auction_interval_hours,
            id="auction-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
        )
    return scheduler


def make_live_sale_job(
    factory: sessionmaker[Session],
    crawler: CapturedSaleCrawler,
    settings: Settings,
    status_verifier: CapturedSaleStatusVerifier | None = None,
) -> Callable[[], None]:
    async def run_sale_cycle() -> None:
        with factory() as session:
            existing_source_count = session.scalar(
                select(func.count())
                .select_from(Property)
                .where(Property.source == "591")
            ) or 0
            async with live_sale_notifier(settings) as notifier:
                result = await ingest_sale_listings(
                    crawler,
                    session,
                    notifier=notifier if existing_source_count > 0 else None,
                    # Channel 1530073991442595880 receives only the daily
                    # county/city count summary, never one message per item.
                    notify_new=False,
                )
                if notifier is not None and existing_source_count > 0:
                    summary = await deliver_daily_sale_summary(
                        session,
                        notifier.new_channel,
                        settings.discord_sale_new_channel_id,
                    )
                    logger.info(
                        "sale daily summary delivery finished: %s",
                        summary,
                    )
            if status_verifier is not None:
                try:
                    status_result = await reconcile_stale_sale_listings(
                        session,
                        status_verifier,
                        missing_days=settings.sale_status_missing_days,
                        limit=settings.sale_status_verify_limit,
                    )
                    logger.info(
                        "sale status reconciliation finished: %s",
                        status_result,
                    )
                except SaleStatusVerificationError:
                    # A verification outage must never turn an unknown
                    # listing into an inactive one or suppress the daily
                    # count summary.
                    logger.exception("sale status reconciliation failed safely")
            logger.info(
                "sale crawl finished: %s baseline=%s",
                result,
                existing_source_count == 0,
            )

    def run_sale_job() -> None:
        asyncio.run(run_sale_cycle())

    return run_sale_job


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
                    summary_report = await deliver_daily_auction_summary(
                        session,
                        notifier.new_channel,
                        settings.discord_auction_new_channel_id,
                    )
                    logger.info("auction daily summary delivery finished: %s", summary_report)
                    round_two_summary_report = await deliver_daily_auction_summary(
                        session,
                        notifier.round_channel,
                        settings.discord_auction_round_channel_id,
                        round_number=2,
                    )
                    logger.info(
                        "auction daily round-two summary delivery finished: %s",
                        round_two_summary_report,
                    )
                    round_three_summary_report = await deliver_daily_auction_summary(
                        session,
                        notifier.round_channel,
                        settings.discord_auction_round_channel_id,
                        round_number=3,
                    )
                    logger.info(
                        "auction daily round-three summary delivery finished: %s",
                        round_three_summary_report,
                    )
                    report = await deliver_pending_notifications(session, notifier, liquidity_index=liquidity_index)
                    logger.info("auction notification delivery finished: %s", report)

    def run_auction_job() -> None:
        asyncio.run(run_auction_cycle())

    return run_auction_job


def main() -> None:
    """Run the production scheduler.

    The long-running entrypoint syncs licensed MOI market data and, when
    configured, runs the operator-approved MOJ Playwright capture adapter.
    Fixture composition remains available only through the test helpers above.
    """

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    market_source = MoiActualPriceSource(cache_dir=settings.moi_cache_dir)
    sale_job: Callable[[], None] | None = None
    if settings.sale_capture_enabled:
        sale_job = make_live_sale_job(
            factory,
            CapturedSaleCrawler(
                settings.sale_capture_script,
                output_dir=settings.sale_capture_output_dir,
                max_pages=settings.sale_capture_max_pages,
            ),
            settings,
            CapturedSaleStatusVerifier(settings.sale_capture_script),
        )
    auction_job: Callable[[], None] | None = None
    if settings.auction_capture_enabled:
        auction_source = CapturedAuctionAnnouncementSource(
            settings.auction_capture_script,
            output_dir=settings.auction_capture_output_dir,
            download_dir=settings.auction_capture_download_dir,
            umi_ocr_url=settings.auction_capture_ocr_url,
            umi_ocr_executable=settings.auction_capture_ocr_executable,
            umi_ocr_startup_timeout_seconds=(
                settings.auction_capture_ocr_startup_timeout_seconds
            ),
        )
        auction_job = make_live_auction_job(
            factory,
            auction_source,
            MojEstateDetailParser(),
            settings,
        )
    scheduler = build_production_scheduler(
        make_market_sync_job(factory, market_source),
        settings.market_sync_interval_hours,
        sale_job=sale_job,
        sale_interval_minutes=settings.sale_crawl_interval_minutes,
        auction_job=auction_job,
        auction_interval_hours=settings.auction_crawl_interval_hours,
    )
    logger.info(
        "scheduler started; market source=official MOI current sales Open Data; "
        "sale capture=%s script=%s; auction capture=%s script=%s",
        "enabled" if sale_job is not None else "disabled",
        settings.sale_capture_script,
        "enabled" if auction_job is not None else "disabled",
        settings.auction_capture_script,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
