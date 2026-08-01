from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.services.sale_status_lifecycle import reconcile_stale_sale_listings
from crawlers.sale.base import SaleListing
from database.repositories.sale import PropertyRepository


class FakeVerifier:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.seen = []

    async def verify(self, items):
        self.seen = items
        return {
            item.id: self.statuses.get(item.source_property_id, "unknown")
            for item in items
        }


def _listing(source_id: str) -> SaleListing:
    return SaleListing(
        source="591",
        source_property_id=source_id,
        url=f"https://sale.591.com.tw/home/house/detail/2/{source_id}.html",
        city="桃園市",
        district="中壢區",
        total_price_twd=10_000_000,
        unit_price_per_ping_twd=300_000,
        building_area_ping=Decimal("30"),
    )


@pytest.mark.asyncio
async def test_reconcile_marks_only_explicitly_inactive_and_keeps_history(
    session_factory,
) -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = PropertyRepository(session)
        active, _, _ = repo.upsert_listing(_listing("100001"))
        inactive, _, _ = repo.upsert_listing(_listing("100002"))
        unknown, _, _ = repo.upsert_listing(_listing("100003"))
        recent, _, _ = repo.upsert_listing(_listing("100004"))
        for item in (active, inactive, unknown):
            item.last_seen_at = now - timedelta(days=4)
        recent.last_seen_at = now - timedelta(days=2)
        session.commit()

        verifier = FakeVerifier(
            {
                "100001": "active",
                "100002": "inactive",
                "100003": "unknown",
            }
        )
        result = await reconcile_stale_sale_listings(
            session,
            verifier,
            missing_days=3,
            now=now,
        )

        assert result.candidates == 3
        assert result.confirmed_active == 1
        assert result.marked_inactive == 1
        assert result.unknown == 1
        assert {item.source_property_id for item in verifier.seen} == {
            "100001",
            "100002",
            "100003",
        }
        assert active.status == "active"
        assert active.last_seen_at == now
        assert inactive.status == "inactive"
        assert unknown.status == "active"
        assert recent.status == "active"
        assert repo.get(inactive.id) is not None


@pytest.mark.asyncio
async def test_reconcile_respects_candidate_limit(session_factory) -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = PropertyRepository(session)
        for index in range(3):
            item, _, _ = repo.upsert_listing(_listing(f"20000{index}"))
            item.last_seen_at = now - timedelta(days=4, minutes=index)
        session.commit()
        verifier = FakeVerifier({})

        result = await reconcile_stale_sale_listings(
            session,
            verifier,
            missing_days=3,
            limit=2,
            now=now,
        )

    assert result.candidates == 2
    assert len(verifier.seen) == 2
