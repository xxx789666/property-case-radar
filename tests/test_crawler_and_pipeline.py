from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.sale import CompliancePolicy, FixtureSaleCrawler
from database.models.common import MarketPrice
from database.models.sale import Property, PropertyPriceHistory


@pytest.mark.asyncio
async def test_fixture_adapter_and_ingestion(session_factory) -> None:
    crawler = FixtureSaleCrawler(Path("tests/fixtures/sale_listings.json"))
    listings = await crawler.fetch()
    assert listings[0].building_area_ping == Decimal("36.5")

    with session_factory() as session:
        session.add(
            MarketPrice(
                city="桃園市",
                district="中壢區",
                building_type="住宅",
                average_unit_price_twd=394_000,
                transaction_count=30,
            )
        )
        session.commit()
        result = await ingest_sale_listings(crawler, session)
        item = session.scalar(select(Property))
        history_count = session.scalar(select(func.count()).select_from(PropertyPriceHistory))

    assert result.processed == result.created == 1
    assert item.discount_rate == Decimal("0.1091")
    assert item.score is not None
    assert history_count == 1


def test_compliance_policy_refuses_bypass() -> None:
    with pytest.raises(ValueError, match="prohibited"):
        CompliancePolicy(bypass_anti_bot=True)
