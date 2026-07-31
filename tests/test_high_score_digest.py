from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select

from apps.services.high_score_digest import (
    RENTAL_KIND,
    SALE_KIND,
    deliver_high_score_digest,
    queue_rental_high_score,
    queue_sale_high_score,
)
from crawlers.rental.base import RentalListing
from crawlers.sale.base import SaleListing
from database.models.common import NotificationLog
from database.repositories.rental import RentalRepository
from database.repositories.sale import PropertyRepository


class FakeChannel:
    def __init__(self, channel_id=123):
        self.channel_id = channel_id
        self.send = AsyncMock()


def add_sale(session, number: int, score: int):
    item, _, _ = PropertyRepository(session).upsert_listing(
        SaleListing(
            source="fixture",
            source_property_id=f"sale-{number}",
            url=f"https://example.invalid/sale-{number}",
            city="桃園市",
            district="中壢區",
            address=f"測試路 {number} 號",
            total_price_twd=10_000_000 + number,
            unit_price_per_ping_twd=300_000,
            building_area_ping=Decimal("30"),
            discount_rate=Decimal("0.1"),
            score=score,
        )
    )
    item.is_backfill = False
    return item


def add_rental(session, number: int, score: int):
    item, _, _ = RentalRepository(session).upsert(
        RentalListing(
            source="fixture",
            source_property_id=f"rent-{number}",
            url=f"https://example.invalid/rent-{number}",
            title=f"測試租屋 {number}",
            city="桃園市",
            district="中壢區",
            monthly_rent_twd=20_000 + number,
            rent_per_ping_twd=1_000,
            area_ping=Decimal("20"),
        )
    )
    item.is_backfill = False
    item.score = score
    item.discount_rate = Decimal("0.1")
    return item


def test_sale_high_score_queue_is_durable_and_deduplicated(session_factory):
    with session_factory() as session:
        item = add_sale(session, 1, 90)
        assert (
            queue_sale_high_score(
                session,
                item,
                created=True,
                price_dropped=False,
            )
            == 1
        )
        assert (
            queue_sale_high_score(
                session,
                item,
                created=True,
                price_dropped=False,
            )
            == 0
        )
        session.commit()
        row = session.scalar(
            select(NotificationLog).where(NotificationLog.kind == SALE_KIND)
        )
        assert row is not None
        assert row.status == "pending"


async def test_sale_digest_sends_one_message_and_keeps_only_top_20(
    session_factory,
):
    with session_factory() as session:
        for number in range(25):
            item = add_sale(session, number, 80 + number)
            queue_sale_high_score(
                session,
                item,
                created=True,
                price_dropped=False,
            )
        session.commit()
        channel = FakeChannel()

        report = await deliver_high_score_digest(
            session,
            channel,
            source="sale",
            limit=20,
        )

        assert report.attempted == 25
        assert report.delivered == 20
        assert report.suppressed == 5
        assert report.discord_messages == 1
        channel.send.assert_awaited_once()
        embed = channel.send.await_args.kwargs["embed"]
        assert "共 **25 筆**" in embed.description
        assert "其餘 5 筆" in embed.description
        statuses = list(
            session.execute(
                select(NotificationLog.status, NotificationLog.channel_id)
                .where(NotificationLog.kind == SALE_KIND)
            )
        )
        assert sum(status == "delivered" for status, _ in statuses) == 20
        assert sum(status == "suppressed" for status, _ in statuses) == 5
        assert {channel_id for _, channel_id in statuses} == {123}


async def test_failed_digest_remains_retryable(session_factory):
    with session_factory() as session:
        item = add_rental(session, 1, 90)
        queue_rental_high_score(
            session,
            item,
            created=True,
            price_dropped=False,
        )
        session.commit()
        channel = FakeChannel()
        channel.send.side_effect = RuntimeError("Discord unavailable")

        report = await deliver_high_score_digest(
            session,
            channel,
            source="rental",
        )

        assert report.failed == 1
        row = session.scalar(
            select(NotificationLog).where(NotificationLog.kind == RENTAL_KIND)
        )
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert "Discord unavailable" in row.last_error
