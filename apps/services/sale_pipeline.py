from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Mapping
import logging

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from crawlers.sale.base import SaleCrawler
from database.models.common import MarketPrice
from database.models.sale import Property, PropertyPriceHistory
from database.repositories.sale import PropertyRepository
from scoring.sale_score import SaleScoreInput, score_sale
from scoring.land_score import LandScoreInput, score_land
from notifications.sale_notification import SaleNotificationRouter
from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
    queue_sale_subscription_matches,
)
from apps.services.high_score_digest import (
    deliver_high_score_digest,
    queue_sale_high_score,
)
from apps.services.performance import measure_stage


logger = logging.getLogger(__name__)

MAX_STORABLE_DISCOUNT_RATE = Decimal("999.9999")
_PRELOAD_BATCH_SIZE = 100


def land_market_type(usage: str | None) -> str | None:
    text = (usage or "").strip()
    categories = set()
    if any(marker in text for marker in ("工業", "丁種建築", "丁建")):
        categories.add("土地:工業用地")
    if any(marker in text for marker in ("農地", "農牧", "農業", "田", "旱")):
        categories.add("土地:農地")
    if any(
        marker in text
        for marker in (
            "建地",
            "住宅用地",
            "商業用地",
            "住宅區",
            "商業區",
            "甲種建築",
            "乙種建築",
            "丙種建築",
            "甲建",
            "乙建",
            "丙建",
        )
    ):
        categories.add("土地:建地")
    return categories.pop() if len(categories) == 1 else None


def _market_type_for_item(item: Property) -> str | None:
    if item.building_type == "土地":
        return land_market_type(item.usage)
    return item.building_type if item.building_type in {"住宅", "店面"} else "住宅"


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
    market_prices: Mapping[tuple[str, str, str], MarketPrice] | None = None,
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
    market = (
        market_prices.get((item.city, item.district, market_type))
        if market_prices is not None
        else session.scalar(
            select(MarketPrice).where(
                MarketPrice.city == item.city,
                MarketPrice.district == item.district,
                MarketPrice.building_type == market_type,
            )
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
    market_prices = {
        (item.city, item.district, item.building_type): item
        for item in session.scalars(select(MarketPrice))
    }
    with measure_stage(logger, "score", "sale-inventory", items=len(items)):
        scored = sum(
            apply_market_score(item, session, market_prices=market_prices)
            for item in items
        )
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
    backfill: bool = False,
    high_score_threshold: int = 80,
    high_score_digest_limit: int = 20,
) -> IngestResult:
    repository = PropertyRepository(session)
    listings = await crawler.fetch()
    keys = {(item.source, item.source_property_id) for item in listings}
    existing_by_key: dict[tuple[str, str], Property] = {}
    if keys:
        key_list = list(keys)
        for offset in range(0, len(key_list), _PRELOAD_BATCH_SIZE):
            batch = key_list[offset : offset + _PRELOAD_BATCH_SIZE]
            existing_by_key.update(
                {
                    (item.source, item.source_property_id): item
                    for item in session.scalars(
                        select(Property).where(
                            tuple_(Property.source, Property.source_property_id).in_(batch)
                        )
                    )
                }
            )
    market_prices = {
        (item.city, item.district, item.building_type): item
        for item in session.scalars(select(MarketPrice))
    }
    created_count = 0
    price_drops = 0
    notification_events: list[tuple[Property, bool, bool, float]] = []
    with measure_stage(logger, "ingest", "sale", items=len(listings)):
        native_items = (
            repository.bulk_upsert_listings(listings)
            if session.bind is not None and session.bind.dialect.name == "postgresql"
            else None
        )
        for listing in listings:
            key = (listing.source, listing.source_property_id)
            previous = existing_by_key.get(key)
            previous_price = (
                previous.total_price_twd if previous else listing.total_price_twd
            )
            if native_items is not None:
                item = native_items[key]
                created = previous is None
                dropped = previous is not None and listing.total_price_twd < previous.total_price_twd
                if previous is None:
                    session.add(PropertyPriceHistory(property=item, total_price_twd=listing.total_price_twd, unit_price_per_ping_twd=listing.unit_price_per_ping_twd))
                elif listing.total_price_twd != previous.total_price_twd:
                    session.add(PropertyPriceHistory(property=item, total_price_twd=listing.total_price_twd, unit_price_per_ping_twd=listing.unit_price_per_ping_twd))
            else:
                item, created, dropped = repository.upsert_listing(listing, existing=previous, flush=False)
            existing_by_key[key] = item
            if created and backfill:
                item.is_backfill = True
            drop_rate = max(0.0, 1 - item.total_price_twd / previous_price)
            created_count += int(created)
            price_drops += int(dropped)
            notification_events.append((item, created, dropped, drop_rate))
        session.flush()
    with measure_stage(logger, "score", "sale", items=len(notification_events)):
        for item, _, _, drop_rate in notification_events:
            apply_market_score(
                item,
                session,
                price_drop_rate=drop_rate,
                market_prices=market_prices,
            )
    for item, created, dropped, _ in notification_events:
        queue_sale_high_score(
            session,
            item,
            created=created,
            price_dropped=dropped,
            threshold=high_score_threshold,
        )
        if created and not backfill:
            queue_sale_subscription_matches(session, item)
    session.commit()
    if notifier is not None:
        for item, created, dropped, _ in notification_events:
            await notifier.publish(
                item,
                created=created,
                price_dropped=dropped,
                send_new=notify_new,
                send_high_score=False,
            )
        await deliver_high_score_digest(
            session,
            notifier.high_score_channel,
            source="sale",
            limit=high_score_digest_limit,
        )
        await deliver_pending_subscription_notifications(
            session, notifier.subscription_channel
        )
    return IngestResult(processed=len(listings), created=created_count, price_drops=price_drops)
