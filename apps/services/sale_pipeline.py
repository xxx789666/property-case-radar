from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.sale.base import SaleCrawler
from database.models.common import MarketPrice
from database.models.sale import Property
from database.repositories.sale import PropertyRepository
from scoring.sale_score import SaleScoreInput, score_sale
from scoring.land_score import LandScoreInput, score_land
from notifications.sale_notification import SaleNotificationRouter
from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
    queue_sale_subscription_matches,
)

MAX_STORABLE_DISCOUNT_RATE = Decimal("999.9999")


def land_market_type(usage: str | None) -> str | None:
    text = (usage or "").strip()
    categories = set()
    if any(marker in text for marker in ("工業", "丁種建築")):
        categories.add("土地:工業用地")
    if any(marker in text for marker in ("農地", "農牧", "農業", "田", "旱")):
        categories.add("土地:農地")
    if any(
        marker in text
        for marker in (
            "建地",
            "住宅用地",
            "商業用地",
            "甲種建築",
            "乙種建築",
            "丙種建築",
        )
    ):
        categories.add("土地:建地")
    return categories.pop() if len(categories) == 1 else None


@dataclass(frozen=True)
class IngestResult:
    processed: int = 0
    created: int = 0
    price_drops: int = 0


@dataclass(frozen=True)
class RescoreResult:
    processed: int = 0
    scored: int = 0
    unmatched: int = 0


def _completeness(item: object) -> float:
    fields = ("address", "age_years", "floor", "layout", "building_type", "usage", "has_parking")
    present = sum(getattr(item, field, None) is not None for field in fields)
    return present / len(fields)


def _land_completeness(item: Property) -> float:
    values = (
        item.address,
        item.land_area_ping,
        item.usage,
        item.total_price_twd,
        item.unit_price_per_ping_twd,
        item.url,
    )
    return sum(value not in (None, "") for value in values) / len(values)


def apply_market_score(
    item: Property,
    session: Session,
    *,
    price_drop_rate: float = 0,
) -> bool:
    if item.building_type == "土地":
        market_type = land_market_type(item.usage)
        if market_type is None:
            item.market_unit_price_twd = None
            item.discount_rate = None
            item.score = None
            return False
    else:
        market_type = (
            item.building_type
            if item.building_type in {"住宅", "店面"}
            else "住宅"
        )
    market = session.scalar(
        select(MarketPrice).where(
            MarketPrice.city == item.city,
            MarketPrice.district == item.district,
            MarketPrice.building_type == market_type,
        )
    )
    if market is None:
        item.market_unit_price_twd = None
        item.discount_rate = None
        item.score = None
        return False

    if item.building_type == "土地":
        score = score_land(
            LandScoreInput(
                listing_unit_price=item.unit_price_per_ping_twd,
                market_unit_price=market.average_unit_price_twd,
                transaction_count=market.transaction_count,
                completeness_ratio=_land_completeness(item),
                price_drop_rate=price_drop_rate,
            )
        )
    else:
        score = score_sale(
            SaleScoreInput(
                listing_unit_price=item.unit_price_per_ping_twd,
                market_unit_price=market.average_unit_price_twd,
                transaction_count=market.transaction_count,
                age_years=(
                    float(item.age_years)
                    if item.age_years is not None
                    else None
                ),
                has_parking=item.has_parking,
                completeness_ratio=_completeness(item),
                price_drop_rate=price_drop_rate,
            )
        )
    if abs(score.discount_rate) > MAX_STORABLE_DISCOUNT_RATE:
        item.market_unit_price_twd = None
        item.discount_rate = None
        item.score = None
        return False

    item.market_unit_price_twd = market.average_unit_price_twd
    item.discount_rate = score.discount_rate
    item.score = score.total
    return True


def rescore_sale_inventory(session: Session) -> RescoreResult:
    items = list(
        session.scalars(
            select(Property).where(Property.status == "active")
        )
    )
    scored = sum(apply_market_score(item, session) for item in items)
    session.commit()
    return RescoreResult(
        processed=len(items),
        scored=scored,
        unmatched=len(items) - scored,
    )


async def ingest_sale_listings(
    crawler: SaleCrawler,
    session: Session,
    *,
    notifier: SaleNotificationRouter | None = None,
    notify_new: bool = True,
) -> IngestResult:
    repository = PropertyRepository(session)
    listings = await crawler.fetch()
    created_count = 0
    price_drops = 0
    notification_events: list[tuple[Property, bool, bool]] = []
    for listing in listings:
        previous = session.scalar(
            select(Property).where(
                Property.source == listing.source,
                Property.source_property_id == listing.source_property_id,
            )
        )
        previous_price = previous.total_price_twd if previous else listing.total_price_twd
        item, created, dropped = repository.upsert_listing(listing)
        drop_rate = max(0.0, 1 - item.total_price_twd / previous_price)
        apply_market_score(item, session, price_drop_rate=drop_rate)
        created_count += int(created)
        price_drops += int(dropped)
        notification_events.append((item, created, dropped))
        if created:
            queue_sale_subscription_matches(session, item)
    session.commit()
    if notifier is not None:
        for item, created, dropped in notification_events:
            await notifier.publish(
                item,
                created=created,
                price_dropped=dropped,
                send_new=notify_new,
            )
        await deliver_pending_subscription_notifications(
            session, notifier.subscription_channel
        )
    return IngestResult(processed=len(listings), created=created_count, price_drops=price_drops)
