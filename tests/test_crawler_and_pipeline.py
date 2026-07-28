from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from apps.services.sale_pipeline import ingest_sale_listings
from crawlers.sale import CompliancePolicy, FixtureSaleCrawler, SaleCrawler, SaleListing
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


@pytest.mark.asyncio
async def test_land_listing_is_not_scored_against_residential_market(
    session_factory,
) -> None:
    class LandCrawler(SaleCrawler):
        async def fetch(self):
            return [
                SaleListing(
                    source="591",
                    source_property_id="land-1",
                    url="https://land.591.com.tw/sale/1",
                    city="桃園市",
                    district="中壢區",
                    total_price_twd=20_000_000,
                    unit_price_per_ping_twd=100_000,
                    building_area_ping=Decimal("200"),
                    land_area_ping=Decimal("200"),
                    building_type="土地",
                    usage="農地",
                )
            ]

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
        await ingest_sale_listings(LandCrawler(), session)
        item = session.scalar(select(Property))

    assert item.building_type == "土地"
    assert item.usage == "農地"
    assert item.land_area_ping == Decimal("200")
    assert item.market_unit_price_twd is None
    assert item.score is None


@pytest.mark.asyncio
async def test_land_listing_uses_land_market_score(session_factory) -> None:
    class LandCrawler(SaleCrawler):
        async def fetch(self):
            return [
                SaleListing(
                    source="591",
                    source_property_id="land-score",
                    url="https://land.591.com.tw/sale/2",
                    city="桃園市",
                    district="中壢區",
                    address="測試段",
                    total_price_twd=14_000_000,
                    unit_price_per_ping_twd=70_000,
                    building_area_ping=Decimal("200"),
                    land_area_ping=Decimal("200"),
                    building_type="土地",
                    usage="農地",
                )
            ]

    with session_factory() as session:
        session.add(
            MarketPrice(
                city="桃園市",
                district="中壢區",
                building_type="土地",
                average_unit_price_twd=100_000,
                transaction_count=20,
            )
        )
        session.commit()
        await ingest_sale_listings(LandCrawler(), session)
        item = session.scalar(select(Property))

    assert item.market_unit_price_twd == 100_000
    assert item.discount_rate == Decimal("0.3000")
    assert item.score == 90


@pytest.mark.asyncio
async def test_extreme_discount_is_left_unscored_instead_of_overflowing_database(
    session_factory,
) -> None:
    class ExtremeCrawler(SaleCrawler):
        async def fetch(self):
            return [
                SaleListing(
                    source="591",
                    source_property_id="extreme-unit-price",
                    url="https://sale.591.com.tw/home/house/detail/2/extreme",
                    city="花蓮縣",
                    district="花蓮市",
                    total_price_twd=8_582_100_000,
                    unit_price_per_ping_twd=309_599_600,
                    building_area_ping=Decimal("27.72"),
                    building_type="電梯大樓",
                    usage="住家",
                )
            ]

    with session_factory() as session:
        session.add(
            MarketPrice(
                city="花蓮縣",
                district="花蓮市",
                building_type="住宅",
                average_unit_price_twd=251_591,
                transaction_count=20,
            )
        )
        session.commit()

        result = await ingest_sale_listings(ExtremeCrawler(), session)
        item = session.scalar(select(Property))

    assert result.created == 1
    assert item.market_unit_price_twd is None
    assert item.discount_rate is None
    assert item.score is None
