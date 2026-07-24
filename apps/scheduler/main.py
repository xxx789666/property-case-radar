import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session

from apps.config import get_settings
from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.sale import FixtureSaleCrawler
from database.models import Base
from database.session import create_db_engine, create_session_factory

logger = logging.getLogger(__name__)


def build_scheduler(job: Callable[[], None], interval_minutes: int) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        job,
        "interval",
        minutes=interval_minutes,
        id="sale-crawler",
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
    fixture = Path("tests/fixtures/sale_listings.json")
    crawler = FixtureSaleCrawler(fixture)

    def run_sale_job() -> None:
        with factory() as session:
            result = asyncio.run(ingest_sale_listings(crawler, session))
            logger.info("sale crawl finished: %s", result)

    scheduler = build_scheduler(run_sale_job, settings.sale_crawl_interval_minutes)
    logger.info("scheduler started; sale source=offline fixture")
    scheduler.start()


if __name__ == "__main__":
    main()
