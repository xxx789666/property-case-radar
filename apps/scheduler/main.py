import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session

from apps.config import get_settings
from apps.services.auction_pipeline import ingest_auction_announcements
from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.auction.court_crawler import FixtureAuctionAnnouncementSource
from crawlers.auction.parser import CourtAnnouncementParser
from crawlers.sale import FixtureSaleCrawler
from database.models import Base
from database.session import create_db_engine, create_session_factory

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

    def run_auction_job() -> None:
        with factory() as session:
            result = asyncio.run(ingest_auction_announcements(auction_source, auction_parser, session))
            logger.info("auction crawl finished: %s", result)

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
