from decimal import Decimal

from apps.discord_bot.service import HouseCommandService, HouseSearchInput
from crawlers.sale.base import SaleListing
from database.repositories.sale import PropertyRepository


def seed(factory):
    with factory() as session:
        item, _, _ = PropertyRepository(session).upsert_listing(
            SaleListing(
                source="fixture",
                source_property_id="p1",
                url="https://example.invalid/p1",
                city="桃園市",
                district="中壢區",
                total_price_twd=12_800_000,
                unit_price_per_ping_twd=351_000,
                building_area_ping=Decimal("36.5"),
                age_years=Decimal("12"),
                market_unit_price_twd=394_000,
                discount_rate=Decimal("0.1091"),
                score=82,
            )
        )
        session.commit()
        return item.id


def test_house_search_detail_compare(session_factory) -> None:
    property_id = seed(session_factory)
    service = HouseCommandService(session_factory)
    results = service.search(
        HouseSearchInput(
            city="桃園市",
            district="中壢區",
            max_price_wan=1500,
            min_area_ping=Decimal("30"),
            max_age_years=Decimal("20"),
            min_discount_percent=Decimal("10"),
        )
    )
    assert [item.id for item in results] == [property_id]
    assert "低於行情：10.9%" in service.compare(property_id)
    assert service.detail(99999) is None


def test_subscribe_and_unsubscribe_is_user_scoped(session_factory) -> None:
    service = HouseCommandService(session_factory)
    subscription = service.subscribe(123, HouseSearchInput(city="桃園市", district="中壢區"))
    assert service.unsubscribe(999, subscription.id) is False
    assert service.unsubscribe(123, subscription.id) is True
    assert service.unsubscribe(123, subscription.id) is False


def test_upsert_tracks_price_drop_without_duplicate_property(session_factory) -> None:
    with session_factory() as session:
        repository = PropertyRepository(session)
        original = SaleListing(
            source="fixture",
            source_property_id="drop-1",
            url="https://example.invalid/drop-1",
            city="桃園市",
            district="中壢區",
            total_price_twd=15_000_000,
            unit_price_per_ping_twd=400_000,
            building_area_ping=Decimal("37.5"),
        )
        item, created, dropped = repository.upsert_listing(original)
        session.commit()
        assert (created, dropped) == (True, False)

        reduced = SaleListing(
            **{
                **original.to_property_values(),
                "total_price_twd": 14_000_000,
                "unit_price_per_ping_twd": 373_333,
            }
        )
        same_item, created, dropped = repository.upsert_listing(reduced)
        session.commit()
        loaded = repository.get(item.id)

    assert same_item.id == item.id
    assert (created, dropped) == (False, True)
    assert [price.total_price_twd for price in loaded.price_history] == [15_000_000, 14_000_000]
