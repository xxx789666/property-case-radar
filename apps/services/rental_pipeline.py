from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.rental.base import RentalCrawler
from database.models.rental import RentalProperty
from database.repositories.rental import RentalRepository
from notifications.rental_notification import RentalNotificationRouter
from scoring.rental_score import RentalScoreInput, score_rental
from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
    queue_rental_subscription_matches,
)


@dataclass(frozen=True)
class RentalIngestResult:
    processed: int = 0
    created: int = 0
    price_drops: int = 0


def _completeness(item: RentalProperty) -> float:
    values = (
        item.title, item.address, item.area_ping, item.layout, item.floor,
        item.rental_type, item.landlord_type, item.features,
    )
    return sum(value not in (None, "") for value in values) / len(values)


def score_inventory_item(item: RentalProperty, session: Session, price_drop_rate: float = 0) -> bool:
    samples = list(
        session.scalars(
            select(RentalProperty.rent_per_ping_twd).where(
                RentalProperty.city == item.city,
                RentalProperty.district == item.district,
                RentalProperty.status == "active",
            )
        )
    )
    if len(samples) < 3:
        item.district_median_rent_per_ping_twd = None
        item.discount_rate = None
        item.score = None
        return False
    district_median = round(median(samples))
    feature_count = len((item.features or "").split("、")) if item.features else 0
    result = score_rental(
        RentalScoreInput(
            monthly_rent_twd=item.monthly_rent_twd,
            rent_per_ping_twd=item.rent_per_ping_twd,
            district_median_rent_per_ping_twd=district_median,
            completeness_ratio=_completeness(item),
            feature_count=feature_count,
            price_drop_rate=price_drop_rate,
        )
    )
    item.district_median_rent_per_ping_twd = district_median
    item.discount_rate = result.discount_rate
    item.score = result.total
    return True


async def ingest_rental_listings(
    crawler: RentalCrawler,
    session: Session,
    *,
    notifier: RentalNotificationRouter | None = None,
    notify_new: bool = False,
    backfill: bool = False,
) -> RentalIngestResult:
    listings = await crawler.fetch()
    repository = RentalRepository(session)
    events: list[tuple[RentalProperty, bool, bool, float]] = []
    created_count = 0
    drops = 0
    for listing in listings:
        previous = session.scalar(
            select(RentalProperty).where(
                RentalProperty.source == listing.source,
                RentalProperty.source_property_id == listing.source_property_id,
            )
        )
        previous_rent = previous.monthly_rent_twd if previous else listing.monthly_rent_twd
        item, created, dropped = repository.upsert(listing)
        if created and backfill:
            item.is_backfill = True
        drop_rate = max(0.0, 1 - item.monthly_rent_twd / previous_rent)
        events.append((item, created, dropped, drop_rate))
        created_count += int(created)
        drops += int(dropped)
    session.flush()
    for item, _, _, drop_rate in events:
        score_inventory_item(item, session, drop_rate)
    for item, created, _, _ in events:
        if created and not backfill:
            queue_rental_subscription_matches(session, item)
    session.commit()
    if notifier:
        for item, created, dropped, _ in events:
            await notifier.publish(
                item,
                created=created,
                price_dropped=dropped,
                send_new=notify_new,
            )
        await deliver_pending_subscription_notifications(
            session, notifier.subscription_channel
        )
    return RentalIngestResult(
        processed=len(listings), created=created_count, price_drops=drops
    )
