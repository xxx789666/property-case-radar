from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from collections.abc import Mapping
import logging

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from crawlers.rental.base import RentalCrawler
from database.models.rental import RentalProperty, RentalPriceHistory
from database.repositories.rental import RentalRepository
from notifications.rental_notification import RentalNotificationRouter
from scoring.rental_score import RentalScoreInput, score_rental
from apps.services.subscription_notifications import (
    deliver_pending_subscription_notifications,
    queue_rental_subscription_matches,
)
from apps.services.high_score_digest import (
    deliver_high_score_digest,
    queue_rental_high_score,
)
from apps.services.performance import measure_stage


logger = logging.getLogger(__name__)

_PRELOAD_BATCH_SIZE = 100


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


def score_inventory_item(
    item: RentalProperty,
    session: Session,
    price_drop_rate: float = 0,
    district_medians: Mapping[tuple[str, str], int | None] | None = None,
) -> bool:
    if district_medians is None:
        samples = list(
            session.scalars(
                select(RentalProperty.rent_per_ping_twd).where(
                    RentalProperty.city == item.city,
                    RentalProperty.district == item.district,
                    RentalProperty.status == "active",
                )
            )
        )
        district_median = round(median(samples)) if len(samples) >= 3 else None
    else:
        district_median = district_medians.get((item.city, item.district))
    if district_median is None:
        item.district_median_rent_per_ping_twd = None
        item.discount_rate = None
        item.score = None
        return False
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
    high_score_threshold: int = 80,
    high_score_digest_limit: int = 20,
) -> RentalIngestResult:
    listings = await crawler.fetch()
    repository = RentalRepository(session)
    keys = {(item.source, item.source_property_id) for item in listings}
    existing_by_key: dict[tuple[str, str], RentalProperty] = {}
    if keys:
        key_list = list(keys)
        for offset in range(0, len(key_list), _PRELOAD_BATCH_SIZE):
            batch = key_list[offset : offset + _PRELOAD_BATCH_SIZE]
            existing_by_key.update(
                {
                    (item.source, item.source_property_id): item
                    for item in session.scalars(
                        select(RentalProperty).where(
                            tuple_(
                                RentalProperty.source,
                                RentalProperty.source_property_id,
                            ).in_(batch)
                        )
                    )
                }
            )
    events: list[tuple[RentalProperty, bool, bool, float]] = []
    created_count = 0
    drops = 0
    with measure_stage(logger, "ingest", "rental", items=len(listings)):
        native_items = (
            repository.bulk_upsert(listings)
            if session.bind is not None and session.bind.dialect.name == "postgresql"
            else None
        )
        for listing in listings:
            key = (listing.source, listing.source_property_id)
            previous = existing_by_key.get(key)
            previous_rent = (
                previous.monthly_rent_twd if previous else listing.monthly_rent_twd
            )
            if native_items is not None:
                item = native_items[key]
                created = previous is None
                dropped = previous is not None and listing.monthly_rent_twd < previous.monthly_rent_twd
                if previous is None or listing.monthly_rent_twd != previous.monthly_rent_twd:
                    session.add(RentalPriceHistory(rental=item, monthly_rent_twd=listing.monthly_rent_twd, rent_per_ping_twd=listing.rent_per_ping_twd))
            else:
                item, created, dropped = repository.upsert(listing, existing=previous, flush=False)
            existing_by_key[key] = item
            if created and backfill:
                item.is_backfill = True
            drop_rate = max(0.0, 1 - item.monthly_rent_twd / previous_rent)
            events.append((item, created, dropped, drop_rate))
            created_count += int(created)
            drops += int(dropped)
        session.flush()
    samples_by_district: dict[tuple[str, str], list[int]] = {}
    for city, district, rent_per_ping in session.execute(
        select(
            RentalProperty.city,
            RentalProperty.district,
            RentalProperty.rent_per_ping_twd,
        ).where(RentalProperty.status == "active")
    ):
        samples_by_district.setdefault((city, district), []).append(rent_per_ping)
    district_medians: dict[tuple[str, str], int | None] = {
        key: round(median(values)) if len(values) >= 3 else None
        for key, values in samples_by_district.items()
    }
    with measure_stage(logger, "score", "rental", items=len(events)):
        for item, _, _, drop_rate in events:
            score_inventory_item(
                item,
                session,
                drop_rate,
                district_medians=district_medians,
            )
    for item, created, dropped, _ in events:
        queue_rental_high_score(
            session,
            item,
            created=created,
            price_dropped=dropped,
            threshold=high_score_threshold,
        )
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
                send_high_score=False,
            )
        await deliver_high_score_digest(
            session,
            notifier.high_score_channel,
            source="rental",
            limit=high_score_digest_limit,
        )
        await deliver_pending_subscription_notifications(
            session, notifier.subscription_channel
        )
    return RentalIngestResult(
        processed=len(listings), created=created_count, price_drops=drops
    )
