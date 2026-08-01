from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from database.models.sale import Property, PropertySubscription
from database.repositories.sale import PropertyRepository, PropertySearch, SubscriptionRepository
from notifications.sale_notification import build_sale_notification


@dataclass(frozen=True)
class HouseSearchInput:
    city: str
    district: str | None = None
    max_price_wan: int | None = None
    min_area_ping: Decimal | None = None
    max_age_years: Decimal | None = None
    min_discount_percent: Decimal | None = None


class HouseCommandService:
    def __init__(self, factory: sessionmaker[Session]):
        self.factory = factory

    def search(self, values: HouseSearchInput, limit: int = 10) -> list[Property]:
        with self.factory() as session:
            return PropertyRepository(session).search(
                PropertySearch(
                    city=values.city,
                    district=values.district,
                    max_total_price_twd=values.max_price_wan * 10_000 if values.max_price_wan else None,
                    min_building_area_ping=values.min_area_ping,
                    max_age_years=values.max_age_years,
                    min_discount_rate=(
                        values.min_discount_percent / 100
                        if values.min_discount_percent is not None
                        else None
                    ),
                    limit=limit,
                )
            )

    def latest(self, limit: int = 10) -> list[Property]:
        with self.factory() as session:
            return PropertyRepository(session).latest(limit)

    def detail(self, property_id: int) -> Property | None:
        with self.factory() as session:
            return PropertyRepository(session).get(property_id)

    def compare(self, property_id: int) -> str | None:
        item = self.detail(property_id)
        if item is None:
            return None
        return build_sale_notification(item).description

    def subscribe(self, user_id: int, values: HouseSearchInput) -> PropertySubscription:
        with self.factory() as session:
            item = SubscriptionRepository(session).create(
                discord_user_id=user_id,
                city=values.city,
                district=values.district,
                max_total_price_twd=values.max_price_wan * 10_000 if values.max_price_wan else None,
                min_building_area_ping=values.min_area_ping,
                max_age_years=values.max_age_years,
                min_discount_rate=(
                    values.min_discount_percent / 100
                    if values.min_discount_percent is not None
                    else None
                ),
            )
            session.commit()
            return item

    def unsubscribe(self, user_id: int, subscription_id: int) -> bool:
        with self.factory() as session:
            changed = SubscriptionRepository(session).deactivate(user_id, subscription_id)
            session.commit()
            return changed
