from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)


class DiscordChannel(Base, TimestampMixin):
    __tablename__ = "discord_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), unique=True)
    discord_channel_id: Mapped[int] = mapped_column(BigInteger, unique=True)


class ActualTransaction(Base, TimestampMixin):
    __tablename__ = "actual_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(32), index=True)
    district: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    total_price_twd: Mapped[int] = mapped_column(BigInteger)
    building_area_ping: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    unit_price_per_ping_twd: Mapped[int] = mapped_column(BigInteger, index=True)
    building_type: Mapped[str | None] = mapped_column(String(64))


class MarketPrice(Base, TimestampMixin):
    __tablename__ = "market_prices"
    __table_args__ = (
        UniqueConstraint("city", "district", "building_type", name="uq_market_area_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(32), index=True)
    district: Mapped[str] = mapped_column(String(32), index=True)
    building_type: Mapped[str] = mapped_column(String(64), default="住宅")
    average_unit_price_twd: Mapped[int] = mapped_column(BigInteger)
    transaction_count: Mapped[int] = mapped_column(default=0)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
