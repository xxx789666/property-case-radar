from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.services.subscription_notifications import (
    _rental_matches,
    _sale_type_matches,
    deliver_pending_subscription_notifications,
    queue_rental_subscription_matches,
    queue_sale_subscription_matches,
)
from database.models.common import NotificationLog
from database.models.rental import RentalProperty, RentalSubscription
from database.models.sale import Property, PropertySubscription


class RecordingChannel:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, *, embed, content=None):
        self.messages.append((content, embed))


@pytest.mark.parametrize(
    ("property_type", "usage"),
    [
        ("building_land", "住宅用地"),
        ("building_land", "商業區"),
        ("building_land", "甲種建築用地"),
        ("building_land", "乙種建築用地"),
        ("building_land", "丙種建築用地"),
        ("building_land", "丁種建築用地"),
        ("type_a_building_land", "甲種建築用地"),
        ("type_b_building_land", "乙種建築用地"),
        ("type_c_building_land", "丙種建築用地"),
        ("type_d_building_land", "丁種建築用地"),
        ("industrial_land", "工業區"),
        ("industrial_land", "丁種建築用地"),
    ],
)
def test_sale_land_type_aliases_match(property_type: str, usage: str) -> None:
    item = Property(building_type="土地", usage=usage)
    assert _sale_type_matches(item, property_type) is True


@pytest.mark.asyncio
async def test_sale_subscription_match_is_durable_deduplicated_and_delivered(
    session_factory,
) -> None:
    with session_factory() as session:
        subscription = PropertySubscription(
            discord_user_id=123456789,
            city="桃園市",
            district="中壢區",
            max_total_price_twd=20_000_000,
            property_type="farmland",
            channel_id=987654321,
            active=True,
        )
        item = Property(
            source="591",
            source_property_id="land-subscription-1",
            url="https://example.invalid/land/1",
            city="桃園市",
            district="中壢區",
            total_price_twd=15_000_000,
            unit_price_per_ping_twd=100_000,
            building_area_ping=Decimal("150"),
            land_area_ping=Decimal("150"),
            building_type="土地",
            usage="農地",
            status="active",
        )
        session.add_all([subscription, item])
        session.flush()

        assert queue_sale_subscription_matches(session, item) == 1
        assert queue_sale_subscription_matches(session, item) == 0
        session.commit()

        channel = RecordingChannel()
        report = await deliver_pending_subscription_notifications(
            session, lambda channel_id: channel
        )
        row = session.scalar(select(NotificationLog))

    assert report.delivered == 1
    assert row.status == "delivered"
    assert channel.messages[0][0] == "<@123456789>"
    assert channel.messages[0][1].title == "🔔 訂閱符合｜一般售屋"


@pytest.mark.asyncio
async def test_rental_subscription_match_is_durable_deduplicated_and_delivered(
    session_factory,
) -> None:
    with session_factory() as session:
        subscription = RentalSubscription(
            discord_user_id=123456789,
            city="桃園市",
            district="中壢區",
            min_monthly_rent_twd=20_000,
            max_monthly_rent_twd=30_000,
            min_area_ping=Decimal("20"),
            rental_type="entire_home",
            layout_contains="2房",
            features_contains="有電梯",
            keywords_any="住辦、店面",
            min_score=80,
            channel_id=987654321,
            active=True,
        )
        item = RentalProperty(
            source="591-rent",
            source_property_id="rental-subscription-1",
            url="https://rent.591.com.tw/12345678",
            title="近捷運住辦兩房",
            city="桃園市",
            district="中壢區",
            monthly_rent_twd=25_000,
            rent_per_ping_twd=1_000,
            area_ping=Decimal("25"),
            layout="2房1廳",
            rental_type="整層住家",
            features="可開伙、有電梯",
            score=85,
            status="active",
        )
        session.add_all([subscription, item])
        session.flush()

        assert _rental_matches(item, subscription) is True
        item.monthly_rent_twd = 19_999
        assert _rental_matches(item, subscription) is False
        item.monthly_rent_twd = 25_000
        item.area_ping = Decimal("19.99")
        assert _rental_matches(item, subscription) is False
        item.area_ping = Decimal("25")
        assert queue_rental_subscription_matches(session, item) == 1
        assert queue_rental_subscription_matches(session, item) == 0
        session.commit()

        channel = RecordingChannel()
        report = await deliver_pending_subscription_notifications(
            session, lambda channel_id: channel
        )
        row = session.scalar(select(NotificationLog))

    assert report.delivered == 1
    assert row.status == "delivered"
    assert channel.messages[0][0] == "<@123456789>"
    assert channel.messages[0][1].title == "🔔 訂閱符合：新租屋"
