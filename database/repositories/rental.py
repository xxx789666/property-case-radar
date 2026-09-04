from datetime import datetime, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crawlers.rental.base import RentalListing
from database.models.rental import RentalPriceHistory, RentalProperty


# PostgreSQL expands tuple-IN predicates into a deeply nested expression tree.
# Keep each statement small enough to avoid max_stack_depth failures on large
# daily crawls (which can contain many thousands of listings).
_PRELOAD_BATCH_SIZE = 100
_UPSERT_BATCH_SIZE = 500


_NOT_PROVIDED = object()


class RentalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        listing: RentalListing,
        *,
        existing: RentalProperty | None | object = _NOT_PROVIDED,
        flush: bool = True,
    ) -> tuple[RentalProperty, bool, bool]:
        item = existing
        if item is _NOT_PROVIDED:
            item = self.session.scalar(
                select(RentalProperty).where(
                    RentalProperty.source == listing.source,
                    RentalProperty.source_property_id == listing.source_property_id,
                )
            )
        now = datetime.now(timezone.utc)
        if item is None:
            item = RentalProperty(
                **listing.to_model_values(),
                first_seen_at=now,
                last_seen_at=now,
            )
            self.session.add(item)
            self.session.add(
                RentalPriceHistory(
                    rental=item,
                    monthly_rent_twd=listing.monthly_rent_twd,
                    rent_per_ping_twd=listing.rent_per_ping_twd,
                    observed_at=now,
                )
            )
            if flush:
                self.session.flush()
            return item, True, False

        assert isinstance(item, RentalProperty)

        dropped = listing.monthly_rent_twd < item.monthly_rent_twd
        changed = listing.monthly_rent_twd != item.monthly_rent_twd
        for key, value in listing.to_model_values().items():
            setattr(item, key, value)
        item.last_seen_at = now
        if changed:
            self.session.add(
                RentalPriceHistory(
                    rental=item,
                    monthly_rent_twd=listing.monthly_rent_twd,
                    rent_per_ping_twd=listing.rent_per_ping_twd,
                    observed_at=now,
                )
            )
        if flush:
            self.session.flush()
        return item, False, dropped

    def bulk_upsert(self, listings: list[RentalListing]) -> dict[tuple[str, str], RentalProperty]:
        """PostgreSQL-native batch upsert with ORM fallback for tests/SQLite."""
        if not listings:
            return {}
        if len(listings) > _UPSERT_BATCH_SIZE and self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            result: dict[tuple[str, str], RentalProperty] = {}
            for offset in range(0, len(listings), _UPSERT_BATCH_SIZE):
                result.update(self.bulk_upsert(listings[offset : offset + _UPSERT_BATCH_SIZE]))
            return result
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            result: dict[tuple[str, str], RentalProperty] = {}
            for listing in listings:
                item, _, _ = self.upsert(listing, flush=False)
                result[(listing.source, listing.source_property_id)] = item
            self.session.flush()
            return result
        now = datetime.now(timezone.utc)
        rows = [{**listing.to_model_values(), "first_seen_at": now, "last_seen_at": now} for listing in listings]
        stmt = pg_insert(RentalProperty).values(rows)
        update_values = {
            key: getattr(stmt.excluded, key)
            for key in rows[0]
            if key not in {"source", "source_property_id", "first_seen_at"}
        }
        update_values["last_seen_at"] = now
        self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_rental_source_id", set_=update_values
            )
        )
        self.session.flush()
        keys = [(x.source, x.source_property_id) for x in listings]
        result: dict[tuple[str, str], RentalProperty] = {}
        for offset in range(0, len(keys), _PRELOAD_BATCH_SIZE):
            batch = keys[offset : offset + _PRELOAD_BATCH_SIZE]
            result.update(
                {
                    (item.source, item.source_property_id): item
                    for item in self.session.scalars(
                        select(RentalProperty).where(
                            tuple_(RentalProperty.source, RentalProperty.source_property_id).in_(batch)
                        )
                    )
                }
            )
        return result
