from datetime import date, datetime, timezone

import pytest

from property_case_radar.auction.models import (
    AuctionCase,
    AuctionDocument,
    AuctionRound,
    CaseType,
    OccupancyStatus,
    OwnershipType,
)
from property_case_radar.shared.market_prices import InMemoryMarketPriceProvider, RegionalMarketPrice


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def sample_round() -> AuctionRound:
    # Numbers match taiwan_real_estate_radar.md section 六's example
    # (底價 980萬／底價單價23.2萬坪／建坪42.3坪／區域成交均價35.8萬坪
    # -> 表面折價率 35.2%), so scoring tests can sanity-check against
    # the spec's own worked example.
    return AuctionRound(
        round_number=2,
        floor_price_total=980.0,
        floor_unit_price=23.2,
        auction_date=date(2026, 8, 18),
        deposit=196.0,
    )


@pytest.fixture
def sample_case(sample_round: AuctionRound) -> AuctionCase:
    return AuctionCase(
        case_id="桃園地方法院:115年度司執字第XXXX號",
        court_name="桃園地方法院",
        case_number="115年度司執字第XXXX號",
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        division="執股",
        case_type=CaseType.RESIDENTIAL,
        city="桃園市",
        district="中壢區",
        address="桃園市中壢區中央路一段OO號",
        announced_date=date(2026, 7, 1),
        building_area_ping=42.3,
        land_area_ping=15.6,
        ownership_ratio="全部",
        ownership_type=OwnershipType.FULL,
        occupancy_status=OccupancyStatus.VACANT_DELIVERABLE,
        occupancy_note="空屋",
        debtor="王小明",
        owner="王小明",
        announcement_url="https://court.example.gov.tw/ann/12345",
        documents=[AuctionDocument(doc_type="announcement", url="https://court.example.gov.tw/ann/12345")],
        rounds=[sample_round],
    )


@pytest.fixture
def market_prices() -> InMemoryMarketPriceProvider:
    return InMemoryMarketPriceProvider(
        [
            RegionalMarketPrice(
                city="桃園市",
                district="中壢區",
                avg_unit_price_per_ping=35.8,
                sample_size=42,
                as_of=date(2026, 6, 30),
            )
        ]
    )
