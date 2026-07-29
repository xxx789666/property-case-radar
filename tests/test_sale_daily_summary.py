from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from apps.services.sale_daily_summary import (
    build_daily_sale_summary_embed,
    daily_sale_counts,
    deliver_daily_sale_summary,
)
from crawlers.sale.base import SaleListing
from database.models.common import NotificationLog
from database.repositories.sale import PropertyRepository

TAIPEI = ZoneInfo("Asia/Taipei")


def _listing(source_id: str, city: str) -> SaleListing:
    return SaleListing(
        source="fixture",
        source_property_id=source_id,
        url=f"https://example.invalid/{source_id}",
        city=city,
        district="測試區",
        total_price_twd=10_000_000,
        unit_price_per_ping_twd=300_000,
        building_area_ping=Decimal("30"),
    )


def _dated_listing(source_id: str, city: str, listed_date: date) -> SaleListing:
    values = _listing(source_id, city).to_property_values()
    values["listed_date"] = listed_date
    return SaleListing(**values)


def test_daily_sale_counts_include_zero_regions(session_factory) -> None:
    day = date(2026, 7, 27)
    with session_factory() as session:
        first, _, _ = PropertyRepository(session).upsert_listing(
            _listing("sale-summary-1", "桃園市")
        )
        second, _, _ = PropertyRepository(session).upsert_listing(
            _listing("sale-summary-2", "台北市")
        )
        first.first_seen_at = datetime(2026, 7, 27, 1, 0, tzinfo=TAIPEI)
        second.first_seen_at = datetime(2026, 7, 27, 23, 0, tzinfo=TAIPEI)
        session.commit()

        counts = daily_sale_counts(session, day)

    assert counts["桃園市"] == 1
    assert counts["臺北市"] == 1
    assert counts["連江縣"] == 0


def test_daily_sale_counts_exclude_backfill_and_old_listed_dates(
    session_factory,
) -> None:
    day = date(2026, 7, 29)
    with session_factory() as session:
        backfill, _, _ = PropertyRepository(session).upsert_listing(
            _listing("backfill", "桃園市")
        )
        old_listing, _, _ = PropertyRepository(session).upsert_listing(
            _dated_listing("old-listed-date", "桃園市", date(2026, 7, 20))
        )
        today_listing, _, _ = PropertyRepository(session).upsert_listing(
            _dated_listing("today-listed-date", "桃園市", day)
        )
        for item in (backfill, old_listing, today_listing):
            item.first_seen_at = datetime(2026, 7, 29, 12, 0, tzinfo=TAIPEI)
        backfill.is_backfill = True
        session.commit()

        counts = daily_sale_counts(session, day)

    assert counts["桃園市"] == 1


def test_daily_sale_summary_embed_lists_counts_and_total() -> None:
    embed = build_daily_sale_summary_embed(
        date(2026, 7, 27),
        {"桃園市": 3, "臺南市": 2},
    )
    assert embed.title == "📊 今日各縣市新增售屋｜2026-07-27"
    assert "桃園市：3 筆" in embed.description
    assert "臺南市：2 筆" in embed.description
    assert embed.footer.text == "今日合計：5 筆"


@pytest.mark.asyncio
async def test_daily_sale_summary_delivery_is_idempotent(session_factory) -> None:
    day = date(2026, 7, 27)
    channel = AsyncMock()
    with session_factory() as session:
        item, _, _ = PropertyRepository(session).upsert_listing(
            _listing("sale-summary-send", "桃園市")
        )
        item.first_seen_at = datetime(2026, 7, 27, 12, 0, tzinfo=TAIPEI)
        session.commit()

        first = await deliver_daily_sale_summary(session, channel, 123, day=day)
        duplicate = await deliver_daily_sale_summary(session, channel, 123, day=day)

        assert first.delivered is True
        assert first.total == 1
        assert duplicate.skipped_duplicate is True
        channel.send.assert_awaited_once()
        log = session.scalar(
            select(NotificationLog).where(
                NotificationLog.delivery_key == "sale:daily-summary:2026-07-27"
            )
        )
        assert log is not None
        assert log.kind == "sale_daily_summary"
        assert log.status == "delivered"
