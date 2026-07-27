"""One-time 591 baseline import without Discord notifications."""

from __future__ import annotations

import asyncio
import argparse
import json

from sqlalchemy import func, select

from apps.config import get_settings
from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.sale.captured_source import CapturedSaleCrawler
from database.models.sale import Property
from database.session import create_db_engine, create_session_factory


async def _run(*, refresh: bool = False, city: str | None = None) -> dict[str, object]:
    settings = get_settings()
    factory = create_session_factory(create_db_engine(settings.database_url))
    with factory() as session:
        existing = session.scalar(
            select(func.count()).select_from(Property).where(Property.source == "591")
        ) or 0
        if existing and not refresh:
            return {"status": "already_seeded", "existing": existing}
        crawler = CapturedSaleCrawler(
            settings.sale_capture_script,
            output_dir=settings.sale_capture_output_dir,
            max_pages=settings.sale_capture_max_pages,
            city=city,
        )
        result = await ingest_sale_listings(crawler, session, notifier=None)
        return {
            "status": "ok",
            "processed": result.processed,
            "created": result.created,
            "price_drops": result.price_drops,
            "notifications_sent": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--city")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(_run(refresh=args.refresh, city=args.city)),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
