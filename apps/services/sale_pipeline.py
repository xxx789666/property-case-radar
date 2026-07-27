from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.sale.base import SaleCrawler
from database.models.common import MarketPrice
from database.models.sale import Property
from database.repositories.sale import PropertyRepository
from scoring.sale_score import SaleScoreInput, score_sale
from notifications.sale_notification import SaleNotificationRouter


@dataclass(frozen=True)
class IngestResult:
    processed: int = 0
    created: int = 0
    price_drops: int = 0


def _completeness(item: object) -> float:
    fields = ("address", "age_years", "floor", "layout", "building_type", "usage", "has_parking")
    present = sum(getattr(item, field, None) is not None for field in fields)
    return present / len(fields)


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
        market = None
        if item.building_type != "土地":
            market = session.scalar(
                select(MarketPrice).where(
                    MarketPrice.city == item.city,
                    MarketPrice.district == item.district,
                    MarketPrice.building_type == (
                        item.building_type
                        if item.building_type in {"住宅", "店面"}
                        else "住宅"
                    ),
                )
            )
        if market:
            drop_rate = max(0.0, 1 - item.total_price_twd / previous_price)
            score = score_sale(
                SaleScoreInput(
                    listing_unit_price=item.unit_price_per_ping_twd,
                    market_unit_price=market.average_unit_price_twd,
                    transaction_count=market.transaction_count,
                    age_years=float(item.age_years) if item.age_years is not None else None,
                    has_parking=item.has_parking,
                    completeness_ratio=_completeness(item),
                    price_drop_rate=drop_rate,
                )
            )
            item.market_unit_price_twd = market.average_unit_price_twd
            item.discount_rate = score.discount_rate
            item.score = score.total
        created_count += int(created)
        price_drops += int(dropped)
        notification_events.append((item, created, dropped))
    session.commit()
    if notifier is not None:
        for item, created, dropped in notification_events:
            await notifier.publish(
                item,
                created=created,
                price_dropped=dropped,
                send_new=notify_new,
            )
    return IngestResult(processed=len(listings), created=created_count, price_drops=price_drops)
