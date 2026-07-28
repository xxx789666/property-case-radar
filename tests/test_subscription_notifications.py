from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
    queue_sale_subscription_matches,
)
from database.models.common import NotificationLog
from database.models.sale import Property, PropertySubscription


class RecordingChannel:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, *, embed, content=None):
        self.messages.append((content, embed))


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
