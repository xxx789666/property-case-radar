from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from scripts.subscription_broker import SubscriptionBroker


def _broker() -> SubscriptionBroker:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    broker = SubscriptionBroker.__new__(SubscriptionBroker)
    broker.engine = engine
    broker.factory = sessionmaker(bind=engine, expire_on_commit=False)
    broker.settings = SimpleNamespace(
        discord_sale_search_channel_id=11,
        discord_auction_search_channel_id=22,
        discord_rental_search_channel_id=33,
    )
    return broker


def test_house_subscription_create_list_duplicate_and_cancel() -> None:
    broker = _broker()
    try:
        created = broker.execute(
            {
                "operation": "house-create",
                "city": "桃園市",
                "district": "中壢區",
                "max_total_price_twd": 15_000_000,
                "property_type": "building_land",
            }
        )
        duplicate = broker.execute(
            {
                "operation": "house-create",
                "city": "桃園市",
                "district": "中壢區",
                "max_total_price_twd": 15_000_000,
                "property_type": "building_land",
            }
        )
        listed = broker.execute({"operation": "list"})
        cancelled = broker.execute(
            {"operation": "cancel", "kind": "house", "id": created.subscription["id"]}
        )
        empty = broker.execute({"operation": "list"})
    finally:
        broker.engine.dispose()

    assert created.operation == "created"
    assert duplicate.operation == "already-exists"
    assert len(listed.subscriptions) == 1
    assert listed.subscriptions[0]["property_type"] == "building_land"
    assert cancelled.changed is True
    assert empty.subscriptions == []


def test_auction_subscription_serializes_conditions() -> None:
    broker = _broker()
    try:
        created = broker.execute(
            {
                "operation": "auction-create",
                "city": "桃園市",
                "district": "中壢區",
                "case_type": "land",
                "max_floor_price_twd": 20_000_000,
                "min_round": 3,
                "require_deliverable": True,
                "min_investment_score": 80,
            }
        )
    finally:
        broker.engine.dispose()

    assert created.subscription["case_type"] == "land"
    assert created.subscription["min_round"] == 3
    assert created.subscription["require_deliverable"] is True


def test_house_subscription_accepts_type_a_building_land() -> None:
    broker = _broker()
    try:
        created = broker.execute(
            {
                "operation": "house-create",
                "city": "桃園市",
                "district": "楊梅區",
                "property_type": "type_a_building_land",
            }
        )
    finally:
        broker.engine.dispose()

    assert created.subscription["property_type"] == "type_a_building_land"


def test_rental_subscription_create_list_duplicate_and_cancel() -> None:
    broker = _broker()
    try:
        payload = {
            "operation": "rental-create",
            "city": "桃園市",
            "district": "中壢區",
            "max_monthly_rent_twd": 30_000,
            "min_area_ping": 20,
            "rental_type": "entire_home",
            "layout_contains": "2房",
            "features_contains": "有電梯",
            "min_score": 80,
        }
        created = broker.execute(payload)
        duplicate = broker.execute(payload)
        listed = broker.execute({"operation": "list"})
        cancelled = broker.execute(
            {
                "operation": "cancel",
                "kind": "rental",
                "id": created.subscription["id"],
            }
        )
        empty = broker.execute({"operation": "list"})
    finally:
        broker.engine.dispose()

    assert created.operation == "created"
    assert created.subscription["max_monthly_rent_twd"] == 30_000
    assert created.subscription["rental_type"] == "entire_home"
    assert duplicate.operation == "already-exists"
    assert listed.subscriptions[0]["kind"] == "rental"
    assert cancelled.changed is True
    assert empty.subscriptions == []
