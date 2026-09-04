from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, desc, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from crawlers.sale.base import SaleListing
from database.models.sale import Property, PropertyPriceHistory, PropertySubscription


_NOT_PROVIDED = object()
_PRELOAD_BATCH_SIZE = 100
_UPSERT_BATCH_SIZE = 500


@dataclass(frozen=True)
class PropertySearch:
    city: str | None = None
    district: str | None = None
    max_total_price_twd: int | None = None
    min_building_area_ping: Decimal | None = None
    max_age_years: Decimal | None = None
    min_discount_rate: Decimal | None = None
    limit: int = 20


class PropertyRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_listing(
        self,
        listing: SaleListing,
        *,
        existing: Property | None | object = _NOT_PROVIDED,
        flush: bool = True,
    ) -> tuple[Property, bool, bool]:
        item = existing
        if item is _NOT_PROVIDED:
            item = self.session.scalar(
                select(Property).where(
                    Property.source == listing.source,
                    Property.source_property_id == listing.source_property_id,
                )
            )
        now = datetime.now(timezone.utc)
        if item is None:
            item = Property(**listing.to_property_values(), first_seen_at=now, last_seen_at=now)
            self.session.add(item)
            self.session.add(
                PropertyPriceHistory(
                    property=item,
                    total_price_twd=listing.total_price_twd,
                    unit_price_per_ping_twd=listing.unit_price_per_ping_twd,
                    observed_at=now,
                )
            )
            if flush:
                self.session.flush()
            return item, True, False

        assert isinstance(item, Property)

        price_dropped = listing.total_price_twd < item.total_price_twd
        price_changed = listing.total_price_twd != item.total_price_twd
        for key, value in listing.to_property_values().items():
            setattr(item, key, value)
        item.last_seen_at = now
        if price_changed:
            self.session.add(
                PropertyPriceHistory(
                    property=item,
                    total_price_twd=listing.total_price_twd,
                    unit_price_per_ping_twd=listing.unit_price_per_ping_twd,
                    observed_at=now,
                )
            )
        if flush:
            self.session.flush()
        return item, False, price_dropped

    def bulk_upsert_listings(self, listings: list[SaleListing]) -> dict[tuple[str, str], Property]:
        """PostgreSQL-native batch upsert; SQLite/test sessions use the ORM fallback."""
        if not listings:
            return {}
        if len(listings) > _UPSERT_BATCH_SIZE and self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            result: dict[tuple[str, str], Property] = {}
            for offset in range(0, len(listings), _UPSERT_BATCH_SIZE):
                result.update(self.bulk_upsert_listings(listings[offset : offset + _UPSERT_BATCH_SIZE]))
            return result
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            result: dict[tuple[str, str], Property] = {}
            for listing in listings:
                item, _, _ = self.upsert_listing(listing, flush=False)
                result[(listing.source, listing.source_property_id)] = item
            self.session.flush()
            return result
        now = datetime.now(timezone.utc)
        rows = []
        for listing in listings:
            rows.append({**listing.to_property_values(), "first_seen_at": now, "last_seen_at": now})
        stmt = pg_insert(Property).values(rows)
        update_values = {
            key: getattr(stmt.excluded, key)
            for key in rows[0]
            if key not in {"source", "source_property_id", "first_seen_at"}
        }
        update_values["last_seen_at"] = now
        self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_property_source_id", set_=update_values
            )
        )
        self.session.flush()
        keys = [(x.source, x.source_property_id) for x in listings]
        result: dict[tuple[str, str], Property] = {}
        for offset in range(0, len(keys), _PRELOAD_BATCH_SIZE):
            batch = keys[offset : offset + _PRELOAD_BATCH_SIZE]
            result.update(
                {
                    (item.source, item.source_property_id): item
                    for item in self.session.scalars(
                        select(Property).where(
                            tuple_(Property.source, Property.source_property_id).in_(batch)
                        )
                    )
                }
            )
        return result

    def get(self, property_id: int) -> Property | None:
        return self.session.scalar(
            select(Property)
            .options(selectinload(Property.price_history))
            .where(Property.id == property_id)
        )

    def search(self, filters: PropertySearch) -> list[Property]:
        stmt: Select[tuple[Property]] = select(Property).where(Property.status == "active")
        if filters.city:
            stmt = stmt.where(Property.city == filters.city)
        if filters.district:
            stmt = stmt.where(Property.district == filters.district)
        if filters.max_total_price_twd is not None:
            stmt = stmt.where(Property.total_price_twd <= filters.max_total_price_twd)
        if filters.min_building_area_ping is not None:
            stmt = stmt.where(Property.building_area_ping >= filters.min_building_area_ping)
        if filters.max_age_years is not None:
            stmt = stmt.where(Property.age_years <= filters.max_age_years)
        if filters.min_discount_rate is not None:
            stmt = stmt.where(Property.discount_rate >= filters.min_discount_rate)
        return list(self.session.scalars(stmt.order_by(desc(Property.score), desc(Property.first_seen_at)).limit(filters.limit)))

    def latest(self, limit: int = 10) -> list[Property]:
        return list(
            self.session.scalars(
                select(Property)
                .where(Property.status == "active")
                .order_by(desc(Property.first_seen_at))
                .limit(limit)
            )
        )


class SubscriptionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **values: object) -> PropertySubscription:
        subscription = PropertySubscription(**values)
        self.session.add(subscription)
        self.session.flush()
        return subscription

    def list_for_user(self, discord_user_id: int) -> list[PropertySubscription]:
        return list(
            self.session.scalars(
                select(PropertySubscription).where(
                    PropertySubscription.discord_user_id == discord_user_id,
                    PropertySubscription.active.is_(True),
                )
            )
        )

    def deactivate(self, discord_user_id: int, subscription_id: int) -> bool:
        item = self.session.scalar(
            select(PropertySubscription).where(
                PropertySubscription.id == subscription_id,
                PropertySubscription.discord_user_id == discord_user_id,
                PropertySubscription.active.is_(True),
            )
        )
        if item is None:
            return False
        item.active = False
        self.session.flush()
        return True
