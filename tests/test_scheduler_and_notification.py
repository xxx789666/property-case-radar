from decimal import Decimal
from unittest.mock import AsyncMock

from apps.scheduler.main import build_scheduler
from crawlers.sale.base import SaleListing
from database.repositories.sale import PropertyRepository
from notifications.sale_notification import SaleNotificationRouter, build_sale_notification


def test_scheduler_registers_single_sale_job() -> None:
    scheduler = build_scheduler(lambda: None, 90)
    job = scheduler.get_job("sale-crawler")
    assert job is not None
    assert job.max_instances == 1


def test_notification_content(session_factory) -> None:
    with session_factory() as session:
        item, _, _ = PropertyRepository(session).upsert_listing(
            SaleListing(
                source="fixture",
                source_property_id="n1",
                url="https://example.invalid/n1",
                city="桃園市",
                district="中壢區",
                total_price_twd=12_800_000,
                unit_price_per_ping_twd=351_000,
                building_area_ping=Decimal("36.5"),
                market_unit_price_twd=394_000,
                discount_rate=Decimal("0.1091"),
                score=82,
            )
        )
        note = build_sale_notification(item)
    assert note.title == "🏠 新增出售案件"
    assert "評分：82／100" in note.description
    assert "低於行情：10.9%" in note.description


async def test_notification_router_uses_configured_routes(session_factory) -> None:
    with session_factory() as session:
        item, _, _ = PropertyRepository(session).upsert_listing(
            SaleListing(
                source="fixture",
                source_property_id="routes",
                url="https://example.invalid/routes",
                city="桃園市",
                district="中壢區",
                total_price_twd=12_800_000,
                unit_price_per_ping_twd=351_000,
                building_area_ping=Decimal("36.5"),
                score=82,
            )
        )
        new_channel = AsyncMock()
        drop_channel = AsyncMock()
        high_channel = AsyncMock()
        router = SaleNotificationRouter(
            new_channel=new_channel,
            price_drop_channel=drop_channel,
            high_score_channel=high_channel,
            high_score_threshold=80,
        )
        kinds = await router.publish(item, created=True, price_dropped=False)
    assert kinds == ["new", "high_score"]
    new_channel.send.assert_awaited_once()
    drop_channel.send.assert_not_called()
    high_channel.send.assert_awaited_once()
