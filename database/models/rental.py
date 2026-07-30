from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base, TimestampMixin, utcnow


class RentalProperty(Base, TimestampMixin):
    __tablename__ = "rental_properties"
    __table_args__ = (
        UniqueConstraint("source", "source_property_id", name="uq_rental_source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_property_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(32), index=True)
    district: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    monthly_rent_twd: Mapped[int] = mapped_column(BigInteger, index=True)
    rent_per_ping_twd: Mapped[int] = mapped_column(BigInteger)
    area_ping: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    layout: Mapped[str | None] = mapped_column(String(32))
    floor: Mapped[str | None] = mapped_column(String(32))
    total_floors: Mapped[int | None] = mapped_column()
    rental_type: Mapped[str | None] = mapped_column(String(32))
    landlord_type: Mapped[str | None] = mapped_column(String(32))
    features: Mapped[str | None] = mapped_column(Text)
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    district_median_rent_per_ping_twd: Mapped[int | None] = mapped_column(BigInteger)
    discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    score: Mapped[int | None] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    price_history: Mapped[list["RentalPriceHistory"]] = relationship(
        back_populates="rental", cascade="all, delete-orphan"
    )


class RentalPriceHistory(Base):
    __tablename__ = "rental_price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    rental_property_id: Mapped[int] = mapped_column(
        ForeignKey("rental_properties.id", ondelete="CASCADE"), index=True
    )
    monthly_rent_twd: Mapped[int] = mapped_column(BigInteger)
    rent_per_ping_twd: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rental: Mapped[RentalProperty] = relationship(back_populates="price_history")


class RentalSubscription(Base, TimestampMixin):
    __tablename__ = "rental_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    city: Mapped[str] = mapped_column(String(32))
    district: Mapped[str | None] = mapped_column(String(32))
    max_monthly_rent_twd: Mapped[int | None] = mapped_column(BigInteger)
    min_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    max_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    rental_type: Mapped[str | None] = mapped_column(String(32))
    layout_contains: Mapped[str | None] = mapped_column(String(32))
    features_contains: Mapped[str | None] = mapped_column(String(64))
    keywords_any: Mapped[str | None] = mapped_column(String(128))
    min_score: Mapped[int | None] = mapped_column()
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
