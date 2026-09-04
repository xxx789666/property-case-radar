import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from apps.config import Settings, get_settings
from apps.services.auction_daily_summary import (
    deliver_daily_auction_summary,
    update_daily_auction_summary,
)
from apps.services.auction_notification_outbox import deliver_pending_notifications
from apps.services.auction_notifier import live_auction_notifier
from apps.services.auction_pipeline import ingest_auction_announcements
from apps.services.sale_notifier import live_sale_notifier
from apps.services.sale_pipeline import ingest_sale_listings, rescore_sale_inventory
from apps.services.sale_daily_summary import deliver_daily_sale_summary
from apps.services.sale_status_lifecycle import reconcile_stale_sale_listings
from apps.services.rental_daily_summary import (
    deliver_daily_rental_summary,
    update_daily_rental_summary,
)
from apps.services.rental_notifier import live_rental_notifier
from apps.services.rental_pipeline import ingest_rental_listings
from apps.services.rental_status_lifecycle import reconcile_stale_rental_listings
from apps.services.system_alerts import update_system_alert
from apps.services.scheduler_job_recovery import SchedulerJobRecovery
from apps.services.deferred_region_retries import DeferredRegionRetryScheduler
from apps.services.performance import measure_stage
from apps.scheduler.instance_lock import (
    SchedulerAlreadyRunning,
    scheduler_instance_lock,
)
from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
)
from crawlers.auction.court_crawler import AuctionAnnouncementSource, RawAnnouncement
from crawlers.auction.captured_source import CapturedAuctionAnnouncementSource
from crawlers.auction.moj_detail_parser import MojEstateDetailParser
from crawlers.auction.parser import CourtAnnouncementParser
from crawlers.transaction.moi_open_data import MoiActualPriceSource, sync_market_prices
from crawlers.sale.captured_source import CapturedSaleCrawler
from crawlers.sale.base import SaleCrawler, SaleListing
from crawlers.sale.captured_status import (
    CapturedSaleStatusVerifier,
    SaleStatusVerificationError,
)
from crawlers.sale.composite import CompositeSaleCrawler
from crawlers.sale.housefun_source import HousefunSaleCrawler, HousefunStatusVerifier
from crawlers.rental.captured_source import CapturedRentalCrawler
from crawlers.rental.base import RentalCrawler, RentalListing
from crawlers.rental.captured_status import (
    CapturedRentalStatusVerifier,
    RentalStatusVerificationError,
)
from database.models.rental import RentalProperty, RentalSubscription
from database.models.sale import Property
from database.session import create_db_engine, create_session_factory
from notifications.auction_notification import AuctionNotificationRouter
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


def attach_failure_alerts(scheduler: BlockingScheduler, settings: Settings) -> None:
    SchedulerJobRecovery(scheduler, settings).attach()


class _RawAnnouncementBatchSource(AuctionAnnouncementSource):
    def __init__(self, announcements: list[RawAnnouncement]) -> None:
        super().__init__()
        self._announcements = announcements

    async def fetch(self) -> list[RawAnnouncement]:
        return list(self._announcements)


class _RentalListingBatchSource(RentalCrawler):
    def __init__(self, listings: list[RentalListing]) -> None:
        super().__init__()
        self._listings = listings

    async def fetch(self) -> list[RentalListing]:
        return list(self._listings)


class _SaleListingBatchSource(SaleCrawler):
    def __init__(self, listings: list[SaleListing]) -> None:
        super().__init__()
        self._listings = listings

    async def fetch(self) -> list[SaleListing]:
        return list(self._listings)


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
    rental_job: Callable[[], None] | None = None,
    sale_interval_minutes: int = 1440,
    auction_job: Callable[[], None] | None = None,
    auction_interval_hours: int = 24,
    daily_hour: int = 13,
    daily_minute: int = 0,
    sale_daily_hour: int = 12,
    sale_daily_minute: int = 0,
    rental_daily_hour: int = 11,
    rental_daily_minute: int = 0,
) -> BlockingScheduler:
    """Build the official-data production scheduler at fixed Taipei times.

    The interval arguments remain in the public signature for compatibility
    with existing callers, but production jobs intentionally use stable
    wall-clock schedules.  Restarts therefore do not shift daily run times.
    """

    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    if sale_job is not None:
        scheduler.add_job(
            sale_job,
            "cron",
            hour=sale_daily_hour,
            minute=sale_daily_minute,
            id="sale-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if rental_job is not None:
        scheduler.add_job(
            rental_job,
            "cron",
            hour=rental_daily_hour,
            minute=rental_daily_minute,
            id="rental-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    scheduler.add_job(
        market_job,
        "cron",
        hour=daily_hour,
        minute=daily_minute,
        id="market-price-sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if auction_job is not None:
        scheduler.add_job(
            auction_job,
            "cron",
            hour=daily_hour,
            minute=daily_minute,
            id="auction-crawler",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    return scheduler


def make_live_sale_job(
    factory: sessionmaker[Session],
    crawler,
    settings: Settings,
    status_verifier: CapturedSaleStatusVerifier | None = None,
    additional_status_verifiers: tuple[tuple[str, object, int], ...] = (),
    deferred_retries: DeferredRegionRetryScheduler | None = None,
) -> Callable[[], None]:
    def run_deferred_sale_retry(
        regions: tuple[str, ...], attempt: int
    ) -> tuple[str, ...]:
        async def retry_cycle() -> tuple[str, ...]:
            sale_source = getattr(crawler, "sources", {}).get("591")
            retry_method = getattr(sale_source, "retry_failed_cities", None)
            if not callable(retry_method):
                return regions
            with factory() as session:
                retry_listings = await retry_method(regions)
                async with live_sale_notifier(settings) as notifier:
                    if retry_listings:
                        result = await ingest_sale_listings(
                            _SaleListingBatchSource(retry_listings),
                            session,
                            notifier=notifier,
                            notify_new=False,
                            high_score_threshold=settings.sale_high_score_threshold,
                            high_score_digest_limit=settings.sale_high_score_digest_limit,
                        )
                        logger.info(
                            "deferred sale failed-city retry %s ingest finished: %s",
                            attempt,
                            result,
                        )
            unresolved = tuple(
                getattr(sale_source, "last_failed_cities", regions)
            )
            update_system_alert(
                settings,
                key="sale-source:591",
                failing=bool(unresolved),
                title="售屋來源 591",
                detail=(
                    "延後重試後仍失敗：" + "、".join(unresolved)
                    if unresolved
                    else "失敗縣市延後補抓完成"
                ),
            )
            return unresolved

        return asyncio.run(retry_cycle())

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
                    high_score_threshold=settings.sale_high_score_threshold,
                    high_score_digest_limit=settings.sale_high_score_digest_limit,
                )

                sale_source = getattr(crawler, "sources", {}).get("591")
                failed_cities = tuple(
                    getattr(sale_source, "last_failed_cities", ())
                )
                retry_method = getattr(sale_source, "retry_failed_cities", None)
                deferred_retry_pending = False
                if failed_cities and callable(retry_method):
                    if deferred_retries is not None:
                        deferred_retry_pending = deferred_retries.schedule(
                            key="sale-591",
                            regions=failed_cities,
                            callback=run_deferred_sale_retry,
                            delay_minutes=settings.sale_failed_retry_delay_minutes,
                            max_attempts=settings.sale_failed_retry_rounds,
                        )
                        if deferred_retry_pending:
                            update_system_alert(
                                settings,
                                key="sale-source:591",
                                failing=True,
                                title="售屋來源 591",
                                detail="已排定延後補抓：" + "、".join(failed_cities),
                            )
                    else:
                        for retry_round in range(
                        1, settings.sale_failed_retry_rounds + 1
                        ):
                            if not failed_cities:
                                break
                            logger.warning(
                                "waiting %s minutes before sale failed-city retry %s/%s: %s",
                                settings.sale_failed_retry_delay_minutes,
                                retry_round,
                                settings.sale_failed_retry_rounds,
                                ", ".join(failed_cities),
                            )
                            await asyncio.sleep(
                                settings.sale_failed_retry_delay_minutes * 60
                            )
                            retry_listings = await retry_method(failed_cities)
                            if retry_listings:
                                retry_result = await ingest_sale_listings(
                                    _SaleListingBatchSource(retry_listings),
                                    session,
                                    notifier=notifier if existing_source_count > 0 else None,
                                    notify_new=False,
                                    high_score_threshold=settings.sale_high_score_threshold,
                                    high_score_digest_limit=settings.sale_high_score_digest_limit,
                                )
                                logger.info(
                                    "sale failed-city retry %s/%s ingest finished: %s",
                                    retry_round,
                                    settings.sale_failed_retry_rounds,
                                    retry_result,
                                )
                            failed_cities = tuple(
                                getattr(sale_source, "last_failed_cities", ())
                            )

                failures = getattr(crawler, "last_failures", {})
                if sale_source is not None:
                    if deferred_retry_pending:
                        failures.pop("591", None)
                    elif failed_cities:
                        failures["591"] = (
                            getattr(sale_source, "last_health_error", None)
                            or "591 部分縣市補抓仍失敗：" + "、".join(failed_cities)
                        )
                    else:
                        failures.pop("591", None)
                if notifier is not None and existing_source_count > 0:
                    with measure_stage(logger, "Discord", "sale-summary"):
                        summary = await deliver_daily_sale_summary(
                            session,
                            notifier.new_channel,
                            settings.discord_sale_new_channel_id,
                        )
                    logger.info(
                        "sale daily summary delivery finished: %s",
                        summary,
                    )
            # Source health is known as soon as capture/retries finish.  Publish
            # recovery now instead of waiting for the potentially hour-long
            # stale-listing verification below.
            for source_name in getattr(crawler, "sources", {}):
                if source_name == "591" and deferred_retry_pending:
                    continue
                error = failures.get(source_name)
                update_system_alert(
                    settings,
                    key=f"sale-source:{source_name}",
                    failing=error is not None,
                    title=f"售屋來源 {source_name}",
                    detail=error or "抓取正常",
                )
            if failures:
                raise RuntimeError(
                    "sale sources remain incomplete after retries: "
                    + "; ".join(
                        f"{name}: {error}" for name, error in failures.items()
                    )
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
            for source_name, verifier, verify_limit in additional_status_verifiers:
                try:
                    extra_status_result = await reconcile_stale_sale_listings(
                        session,
                        verifier,
                        missing_days=settings.sale_status_missing_days,
                        limit=verify_limit,
                        source=source_name,
                    )
                    logger.info(
                        "%s sale status reconciliation finished: %s",
                        source_name,
                        extra_status_result,
                    )
                except Exception:
                    logger.exception(
                        "%s sale status reconciliation failed safely",
                        source_name,
                    )
            logger.info(
                "sale crawl finished: %s baseline=%s",
                result,
                existing_source_count == 0,
            )

    def run_sale_job() -> None:
        asyncio.run(run_sale_cycle())

    return run_sale_job


def make_live_rental_job(
    factory: sessionmaker[Session],
    crawler: CapturedRentalCrawler,
    settings: Settings,
    status_verifier: CapturedRentalStatusVerifier | None = None,
    deferred_retries: DeferredRegionRetryScheduler | None = None,
) -> Callable[[], None]:
    async def run_rental_cycle() -> None:
        with factory() as session:
            focus_districts = list(
                session.execute(
                    select(
                        RentalSubscription.city,
                        RentalSubscription.district,
                    )
                    .where(
                        RentalSubscription.active.is_(True),
                        RentalSubscription.district.is_not(None),
                    )
                    .distinct()
                ).tuples()
            )
            crawler.set_focus_districts(
                [
                    (city, district)
                    for city, district in focus_districts
                    if district is not None
                ]
            )
            existing_source_count = session.scalar(
                select(func.count())
                .select_from(RentalProperty)
                .where(RentalProperty.source == "591-rent")
            ) or 0
            baseline = existing_source_count == 0
            async with live_rental_notifier(settings) as notifier:
                result = await ingest_rental_listings(
                    crawler,
                    session,
                    notifier=notifier if not baseline else None,
                    notify_new=False,
                    backfill=baseline,
                    high_score_threshold=settings.rental_high_score_threshold,
                    high_score_digest_limit=settings.rental_high_score_digest_limit,
                )
                failed_cities = tuple(crawler.last_failed_cities)
                summary = None
                if notifier is not None and not baseline:
                    with measure_stage(logger, "Discord", "rental-summary"):
                        summary = await deliver_daily_rental_summary(
                            session,
                            notifier.new_channel,
                            settings.discord_rental_new_channel_id,
                            failed_regions=(),
                        )
                    logger.info("rental daily summary delivery finished: %s", summary)

                retry_method = getattr(crawler, "retry_failed_cities", None)
                if failed_cities:
                    update_system_alert(
                        settings,
                        key="rental-source:591",
                        failing=True,
                        title="租屋來源 591",
                        detail=crawler.last_health_error or "部分縣市抓取失敗／待重試",
                    )
                if failed_cities and callable(retry_method):
                    if deferred_retries is not None:
                        def deferred_callback(
                            regions: tuple[str, ...], attempt: int
                        ) -> tuple[str, ...]:
                            async def retry_cycle() -> tuple[str, ...]:
                                with factory() as retry_session:
                                    retry_listings = await retry_method(regions)
                                    async with live_rental_notifier(settings) as retry_notifier:
                                        if retry_listings:
                                            retry_result = await ingest_rental_listings(
                                                _RentalListingBatchSource(retry_listings),
                                                retry_session,
                                                notifier=(
                                                    retry_notifier
                                                    if not baseline
                                                    else None
                                                ),
                                                notify_new=False,
                                                backfill=baseline,
                                                high_score_threshold=settings.rental_high_score_threshold,
                                                high_score_digest_limit=settings.rental_high_score_digest_limit,
                                            )
                                            logger.info(
                                                "deferred rental failed-city retry %s ingest finished: %s",
                                                attempt,
                                                retry_result,
                                            )
                                        unresolved = tuple(crawler.last_failed_cities)
                                        if (
                                            retry_notifier is not None
                                            and summary is not None
                                            and summary.message_id is not None
                                        ):
                                            await update_daily_rental_summary(
                                                retry_session,
                                                retry_notifier.new_channel,
                                                summary.message_id,
                                                day=summary.day,
                                                failed_regions=(),
                                            )
                                update_system_alert(
                                    settings,
                                    key="rental-source:591",
                                    failing=bool(unresolved),
                                    title="租屋來源 591",
                                    detail=(
                                        "延後重試後仍失敗：" + "、".join(unresolved)
                                        if unresolved
                                        else "失敗縣市延後補抓完成"
                                    ),
                                )
                                return unresolved

                            return asyncio.run(retry_cycle())

                        deferred_retries.schedule(
                            key="rental-591",
                            regions=failed_cities,
                            callback=deferred_callback,
                            delay_minutes=settings.rental_failed_retry_delay_minutes,
                            max_attempts=settings.rental_failed_retry_rounds,
                        )
                    else:
                        for retry_round in range(
                            1, settings.rental_failed_retry_rounds + 1
                        ):
                            if not failed_cities:
                                break
                            logger.warning(
                                "waiting %s minutes before rental failed-city retry %s/%s: %s",
                                settings.rental_failed_retry_delay_minutes,
                                retry_round,
                                settings.rental_failed_retry_rounds,
                                ", ".join(failed_cities),
                            )
                            await asyncio.sleep(
                                settings.rental_failed_retry_delay_minutes * 60
                            )
                            retry_listings = await retry_method(failed_cities)
                            if retry_listings:
                                retry_result = await ingest_rental_listings(
                                    _RentalListingBatchSource(retry_listings),
                                    session,
                                    notifier=notifier if not baseline else None,
                                    notify_new=False,
                                    backfill=baseline,
                                    high_score_threshold=settings.rental_high_score_threshold,
                                    high_score_digest_limit=settings.rental_high_score_digest_limit,
                                )
                                logger.info(
                                    "rental failed-city retry %s/%s ingest finished: %s",
                                    retry_round,
                                    settings.rental_failed_retry_rounds,
                                    retry_result,
                                )
                            failed_cities = tuple(crawler.last_failed_cities)
                            if (
                                notifier is not None
                                and not baseline
                                and summary is not None
                                and summary.message_id is not None
                            ):
                                update_report = await update_daily_rental_summary(
                                    session,
                                    notifier.new_channel,
                                    summary.message_id,
                                    day=summary.day,
                                    failed_regions=(),
                                )
                                logger.info(
                                    "rental daily summary updated after retry %s/%s: %s",
                                    retry_round,
                                    settings.rental_failed_retry_rounds,
                                    update_report,
                                )
            if status_verifier is not None:
                try:
                    for source in ("591-rent", "591-business"):
                        verify_limit = (
                            settings.rental_business_status_verify_limit
                            if source == "591-business"
                            else settings.rental_status_verify_limit
                        )
                        if verify_limit == 0:
                            continue
                        status_result = await reconcile_stale_rental_listings(
                            session,
                            status_verifier,
                            missing_days=settings.rental_status_missing_days,
                            limit=verify_limit,
                            source=source,
                        )
                        logger.info(
                            "rental status reconciliation finished: source=%s %s",
                            source,
                            status_result,
                        )
                except RentalStatusVerificationError:
                    logger.exception("rental status reconciliation failed safely")
            update_system_alert(
                settings,
                key="rental-source:591",
                failing=bool(crawler.last_failed_cities),
                title="租屋來源 591",
                detail=(
                    crawler.last_health_error
                    or "失敗縣市補抓完成，591 租屋來源已恢復"
                ),
            )
            logger.info("rental crawl finished: %s baseline=%s", result, baseline)

    def run_rental_job() -> None:
        asyncio.run(run_rental_cycle())

    return run_rental_job


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
            score_report = rescore_sale_inventory(session)
            logger.info(
                "sale inventory rescore finished after MOI sync: %s",
                score_report,
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
            with measure_stage(logger, "ingest", "auction") as measurement:
                result = await ingest_auction_announcements(
                    source,
                    parser,
                    session,
                    liquidity_index=liquidity_index,
                )
                measurement.items = result.processed
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
    deferred_retries: DeferredRegionRetryScheduler | None = None,
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
            with measure_stage(logger, "ingest", "auction") as measurement:
                result = await ingest_auction_announcements(
                    source,
                    parser,
                    session,
                    liquidity_index=liquidity_index,
                )
                measurement.items = result.processed
            logger.info("auction crawl finished: %s", result)
            failed_regions = tuple(getattr(source, "last_failed_counties", ()))
            if failed_regions:
                logger.warning(
                    "auction county capture incomplete; failed regions=%s",
                    ", ".join(failed_regions),
                )
            summary_report = None
            round_two_summary_report = None
            round_three_summary_report = None
            async with live_auction_notifier(settings) as notifier:
                if notifier is not None:
                    with measure_stage(logger, "Discord", "auction-summary"):
                        summary_report = await deliver_daily_auction_summary(
                            session,
                            notifier.new_channel,
                            settings.discord_auction_new_channel_id,
                            failed_regions=failed_regions,
                        )
                    logger.info("auction daily summary delivery finished: %s", summary_report)
                    round_two_summary_report = await deliver_daily_auction_summary(
                        session,
                        notifier.round_channel,
                        settings.discord_auction_round_channel_id,
                        round_number=2,
                        failed_regions=failed_regions,
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
                        failed_regions=failed_regions,
                    )
                    logger.info(
                        "auction daily round-three summary delivery finished: %s",
                        round_three_summary_report,
                    )
                    report = await deliver_pending_notifications(session, notifier, liquidity_index=liquidity_index)
                    logger.info("auction notification delivery finished: %s", report)
                    subscription_report = await deliver_pending_subscription_notifications(
                        session, notifier.subscription_channel
                    )
                    logger.info(
                        "auction subscription delivery finished: %s",
                        subscription_report,
                    )

            retry_method = getattr(source, "retry_failed_counties", None)
            if not failed_regions or not callable(retry_method):
                update_system_alert(
                    settings,
                    key="auction-failed-counties",
                    failing=bool(failed_regions),
                    title="法拍縣市抓取",
                    detail=(
                        f"仍失敗：{', '.join(failed_regions)}"
                        if failed_regions
                        else "22 縣市皆完成"
                    ),
                )
                return

            summary_reports = (
                (summary_report, "new", None),
                (round_two_summary_report, "round", 2),
                (round_three_summary_report, "round", 3),
            )

            if deferred_retries is not None:
                def deferred_callback(
                    regions: tuple[str, ...], attempt: int
                ) -> tuple[str, ...]:
                    async def retry_cycle() -> tuple[str, ...]:
                        with factory() as retry_session:
                            retry_announcements = await retry_method(regions)
                            if retry_announcements:
                                retry_result = await ingest_auction_announcements(
                                    _RawAnnouncementBatchSource(retry_announcements),
                                    parser,
                                    retry_session,
                                    liquidity_index=liquidity_index,
                                )
                                logger.info(
                                    "deferred auction failed-county retry %s ingest finished: %s",
                                    attempt,
                                    retry_result,
                                )
                            unresolved = tuple(
                                getattr(source, "last_failed_counties", ())
                            )
                            async with live_auction_notifier(settings) as retry_notifier:
                                if retry_notifier is not None:
                                    for prior_report, channel_kind, round_number in summary_reports:
                                        if (
                                            prior_report is None
                                            or prior_report.message_id is None
                                        ):
                                            continue
                                        channel = (
                                            retry_notifier.new_channel
                                            if channel_kind == "new"
                                            else retry_notifier.round_channel
                                        )
                                        await update_daily_auction_summary(
                                            retry_session,
                                            channel,
                                            prior_report.message_id,
                                            day=prior_report.day,
                                            round_number=round_number,
                                            failed_regions=unresolved,
                                        )
                                    await deliver_pending_notifications(
                                        retry_session,
                                        retry_notifier,
                                        liquidity_index=liquidity_index,
                                    )
                                    await deliver_pending_subscription_notifications(
                                        retry_session,
                                        retry_notifier.subscription_channel,
                                    )
                        update_system_alert(
                            settings,
                            key="auction-failed-counties",
                            failing=bool(unresolved),
                            title="法拍縣市抓取",
                            detail=(
                                "延後重試後仍失敗：" + "、".join(unresolved)
                                if unresolved
                                else "失敗縣市延後補抓完成"
                            ),
                        )
                        return unresolved

                    return asyncio.run(retry_cycle())

                deferred_retries.schedule(
                    key="auction-counties",
                    regions=failed_regions,
                    callback=deferred_callback,
                    delay_minutes=settings.auction_failed_retry_delay_minutes,
                    max_attempts=settings.auction_failed_retry_rounds,
                )
                update_system_alert(
                    settings,
                    key="auction-failed-counties",
                    failing=True,
                    title="法拍縣市抓取",
                    detail="已排定延後補抓：" + "、".join(failed_regions),
                )
                return

            for retry_round in range(1, settings.auction_failed_retry_rounds + 1):
                if not failed_regions:
                    break
                logger.warning(
                    "waiting %s minutes before auction failed-county retry %s/%s: %s",
                    settings.auction_failed_retry_delay_minutes,
                    retry_round,
                    settings.auction_failed_retry_rounds,
                    ", ".join(failed_regions),
                )
                await asyncio.sleep(settings.auction_failed_retry_delay_minutes * 60)
                try:
                    retry_announcements = await retry_method(failed_regions)
                except Exception as error:  # noqa: BLE001 - classify before retrying
                    logger.exception(
                        "auction failed-county retry %s/%s crashed",
                        retry_round,
                        settings.auction_failed_retry_rounds,
                    )
                    if getattr(error, "retryable", True) is False:
                        raise
                    continue

                if retry_announcements:
                    retry_result = await ingest_auction_announcements(
                        _RawAnnouncementBatchSource(retry_announcements),
                        parser,
                        session,
                        liquidity_index=liquidity_index,
                    )
                    logger.info(
                        "auction failed-county retry %s/%s ingest finished: %s",
                        retry_round,
                        settings.auction_failed_retry_rounds,
                        retry_result,
                    )
                failed_regions = tuple(getattr(source, "last_failed_counties", ()))

                async with live_auction_notifier(settings) as retry_notifier:
                    if retry_notifier is None:
                        continue
                    for prior_report, channel_kind, round_number in summary_reports:
                        if prior_report is None or prior_report.message_id is None:
                            continue
                        channel = (
                            retry_notifier.new_channel
                            if channel_kind == "new"
                            else retry_notifier.round_channel
                        )
                        update_report = await update_daily_auction_summary(
                            session,
                            channel,
                            prior_report.message_id,
                            day=prior_report.day,
                            round_number=round_number,
                            failed_regions=failed_regions,
                        )
                        logger.info(
                            "auction daily summary updated after retry %s/%s: %s",
                            retry_round,
                            settings.auction_failed_retry_rounds,
                            update_report,
                        )
                    delivery_report = await deliver_pending_notifications(
                        session,
                        retry_notifier,
                        liquidity_index=liquidity_index,
                    )
                    logger.info(
                        "auction retry notification delivery finished: %s",
                        delivery_report,
                    )
                    await deliver_pending_subscription_notifications(
                        session, retry_notifier.subscription_channel
                    )
            update_system_alert(
                settings,
                key="auction-failed-counties",
                failing=bool(failed_regions),
                title="法拍縣市抓取",
                detail=(
                    f"3 輪重試後仍失敗：{', '.join(failed_regions)}"
                    if failed_regions
                    else "重試後全部成功"
                ),
            )
            if failed_regions:
                raise RuntimeError(
                    "auction counties remain incomplete after retries: "
                    + ", ".join(failed_regions)
                )

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
    deferred_retries = DeferredRegionRetryScheduler()
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    market_source = MoiActualPriceSource(
        cache_dir=settings.moi_cache_dir,
        history_years=settings.moi_history_years,
    )
    sale_job: Callable[[], None] | None = None
    if settings.sale_capture_enabled:
        sale_sources = {
            "591": CapturedSaleCrawler(
                settings.sale_capture_script,
                output_dir=settings.sale_capture_output_dir,
                max_pages=settings.sale_capture_max_pages,
                workers=settings.sale_capture_workers,
                json_retention_days=settings.sale_capture_json_retention_days,
            )
        }
        additional_status_verifiers = ()
        if settings.housefun_capture_enabled:
            sale_sources["housefun"] = HousefunSaleCrawler(
                max_pages=settings.housefun_capture_max_pages
            )
            additional_status_verifiers = (
                (
                    "housefun",
                    HousefunStatusVerifier(),
                    settings.housefun_status_verify_limit,
                ),
            )
        sale_job = make_live_sale_job(
            factory,
            CompositeSaleCrawler(sale_sources),
            settings,
            CapturedSaleStatusVerifier(
                settings.sale_capture_script,
                workers=settings.sale_status_verify_workers,
            ),
            additional_status_verifiers,
            deferred_retries,
        )
    rental_job: Callable[[], None] | None = None
    if settings.rental_capture_enabled:
        rental_crawler = CapturedRentalCrawler(
            settings.rental_capture_script,
            output_dir=settings.rental_capture_output_dir,
            max_pages=settings.rental_capture_max_pages,
            other_max_pages=settings.rental_capture_other_max_pages,
            focus_max_pages=settings.rental_capture_focus_max_pages,
            workers=settings.rental_capture_workers,
            json_retention_days=settings.rental_capture_json_retention_days,
        )
        rental_job = make_live_rental_job(
            factory,
            rental_crawler,
            settings,
            CapturedRentalStatusVerifier(
                settings.rental_capture_script,
                workers=settings.rental_status_verify_workers,
            ),
            deferred_retries,
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
            json_retention_days=settings.auction_capture_json_retention_days,
        )
        auction_job = make_live_auction_job(
            factory,
            auction_source,
            MojEstateDetailParser(),
            settings,
            deferred_retries=deferred_retries,
        )
    scheduler = build_production_scheduler(
        make_market_sync_job(factory, market_source),
        settings.market_sync_interval_hours,
        sale_job=sale_job,
        rental_job=rental_job,
        sale_interval_minutes=settings.sale_crawl_interval_minutes,
        auction_job=auction_job,
        auction_interval_hours=settings.auction_crawl_interval_hours,
        daily_hour=settings.scheduler_daily_hour,
        daily_minute=settings.scheduler_daily_minute,
        sale_daily_hour=settings.sale_scheduler_daily_hour,
        sale_daily_minute=settings.sale_scheduler_daily_minute,
        rental_daily_hour=settings.rental_scheduler_daily_hour,
        rental_daily_minute=settings.rental_scheduler_daily_minute,
    )
    deferred_retries.bind(scheduler)
    attach_failure_alerts(scheduler, settings)
    logger.info(
        "scheduler started; market source=official MOI current sales Open Data; "
        "sale capture=%s script=%s; rental capture=%s script=%s; "
        "auction capture=%s script=%s; sale schedule=%02d:%02d; "
        "rental schedule=%02d:%02d; market/auction schedule=%02d:%02d Asia/Taipei",
        "enabled" if sale_job is not None else "disabled",
        settings.sale_capture_script,
        "enabled" if rental_job is not None else "disabled",
        settings.rental_capture_script,
        "enabled" if auction_job is not None else "disabled",
        settings.auction_capture_script,
        settings.sale_scheduler_daily_hour,
        settings.sale_scheduler_daily_minute,
        settings.rental_scheduler_daily_hour,
        settings.rental_scheduler_daily_minute,
        settings.scheduler_daily_hour,
        settings.scheduler_daily_minute,
    )
    try:
        with scheduler_instance_lock(Path("logs/scheduler.lock")):
            scheduler.start()
    except SchedulerAlreadyRunning:
        logger.warning("scheduler startup skipped: another instance is running")


if __name__ == "__main__":
    main()
