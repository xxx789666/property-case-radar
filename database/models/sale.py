from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base, TimestampMixin, utcnow


class Property(Base, TimestampMixin):
    __tablename__ = "properties"
    __table_args__ = (UniqueConstraint("source", "source_property_id", name="uq_property_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_property_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(String(32), index=True)
    district: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    total_price_twd: Mapped[int] = mapped_column(BigInteger, index=True)
    unit_price_per_ping_twd: Mapped[int] = mapped_column(BigInteger)
    building_area_ping: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    land_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    age_years: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    floor: Mapped[str | None] = mapped_column(String(32))
    total_floors: Mapped[int | None]
    layout: Mapped[str | None] = mapped_column(String(32))
    building_type: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[str | None] = mapped_column(String(64))
    has_parking: Mapped[bool | None] = mapped_column(Boolean)
    listed_date: Mapped[date | None] = mapped_column(Date)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    market_unit_price_twd: Mapped[int | None] = mapped_column(BigInteger)
    discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    score: Mapped[int | None] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    price_history: Mapped[list["PropertyPriceHistory"]] = relationship(
        back_populates="property", cascade="all, delete-orphan"
    )


class PropertyPriceHistory(Base):
    __tablename__ = "property_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    total_price_twd: Mapped[int] = mapped_column(BigInteger)
    unit_price_per_ping_twd: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    property: Mapped[Property] = relationship(back_populates="price_history")


class PropertySubscription(Base, TimestampMixin):
    __tablename__ = "property_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    city: Mapped[str] = mapped_column(String(32))
    district: Mapped[str | None] = mapped_column(String(32))
    max_total_price_twd: Mapped[int | None] = mapped_column(BigInteger)
    min_building_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    max_age_years: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    min_discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
