from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.services.rental_status_lifecycle import reconcile_stale_rental_listings
from database.models.base import Base
from database.models.rental import RentalProperty


class FakeVerifier:
    async def verify(self, items):
        return {items[0].id: "inactive", items[1].id: "unknown"}


def rental(source_id: str, seen_at: datetime) -> RentalProperty:
    return RentalProperty(
        source="591-rent",
        source_property_id=source_id,
        url=f"https://rent.591.com.tw/{source_id}",
        title="租屋",
        city="桃園市",
        district="中壢區",
        monthly_rent_twd=20_000,
        rent_per_ping_twd=1_000,
        area_ping=Decimal("20"),
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        status="active",
    )


async def test_only_explicit_inactive_status_marks_rental_inactive() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        first = rental("1", now - timedelta(days=4))
        second = rental("2", now - timedelta(days=4))
        session.add_all([first, second])
        session.commit()
        result = await reconcile_stale_rental_listings(
            session, FakeVerifier(), now=now
        )
        assert result.marked_inactive == 1
        assert result.unknown == 1
        assert first.status == "inactive"
        assert second.status == "active"
