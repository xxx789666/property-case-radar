from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.rental.base import RentalListing
from database.models.rental import RentalPriceHistory, RentalProperty


class RentalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, listing: RentalListing) -> tuple[RentalProperty, bool, bool]:
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
            self.session.flush()
            self.session.add(
                RentalPriceHistory(
                    rental_property_id=item.id,
                    monthly_rent_twd=listing.monthly_rent_twd,
                    rent_per_ping_twd=listing.rent_per_ping_twd,
                    observed_at=now,
                )
            )
            return item, True, False

        dropped = listing.monthly_rent_twd < item.monthly_rent_twd
        changed = listing.monthly_rent_twd != item.monthly_rent_twd
        for key, value in listing.to_model_values().items():
            setattr(item, key, value)
        item.last_seen_at = now
        if changed:
            self.session.add(
                RentalPriceHistory(
                    rental_property_id=item.id,
                    monthly_rent_twd=listing.monthly_rent_twd,
                    rent_per_ping_twd=listing.rent_per_ping_twd,
                    observed_at=now,
                )
            )
        self.session.flush()
        return item, False, dropped
