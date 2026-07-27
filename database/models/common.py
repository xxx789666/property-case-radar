from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base, TimestampMixin, utcnow


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
    """Shared notification *outbox* for both pipelines.

    Exactly one of ``property_id`` (sale) / ``auction_case_id`` (auction)
    is expected for per-item rows. Daily aggregate rows intentionally leave
    both null and use ``kind="daily_summary"``. This is not enforced with a DB CHECK
    constraint in v1 (SQLite's ALTER TABLE support makes that annoying to
    retrofit; revisit once this table needs stricter guarantees), but
    every writer should treat it as an invariant.

    This is a durable outbox, not just an audit log:
    ``apps.services.auction_notification_outbox.queue_pending_notifications``
    inserts a ``status="pending"`` row for every (case/event, channel
    kind) destination inside the SAME transaction as the domain mutation
    it describes, so the pending notification and the data it's about
    are always committed atomically. Actual delivery happens strictly
    after that commit (see ``deliver_pending_notifications``), is safe to
    retry (``attempt_count``/``last_error`` track bounded retries across
    scheduler runs -- including recovering from a process crash between
    commit and delivery, since delivery re-queries every undelivered row
    rather than trusting an in-memory list), and is idempotent per
    ``delivery_key`` (unique) so a re-ingested, already-processed
    announcement can never queue -- or resend -- a duplicate.
    """

    __tablename__ = "notification_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"))
    auction_case_id: Mapped[int | None] = mapped_column(ForeignKey("auction_cases.id"))
    # Which specific auction_status_history row this notification renders
    # (None for a "new case" notification, which has no single event).
    # Storing this -- rather than just the case -- lets delivery re-render
    # the exact notification from DB state alone, even in a different
    # process/run than the one that queued it.
    status_history_id: Mapped[int | None] = mapped_column(ForeignKey("auction_status_history.id"))
    # Resolved and stamped only once actually delivered -- at queue time
    # we only know the destination *kind* ("new"/"round"/...), not which
    # concrete Discord channel ID that resolves to (that's a delivery-time
    # concern, decoupling stored rows from a specific channel config).
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    delivery_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | delivered | failed | suppressed | held
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("delivery_key", name="uq_notification_logs_delivery_key"),)

    def __init__(self, **kwargs: object) -> None:
        # See database.models.auction.AuctionCase.__init__ for why this
        # override exists: mapped_column(default=...) is a flush/INSERT-
        # time default, not a Python-construction-time one -- callers
        # (apps.services.auction_notification_outbox) read `.status`
        # immediately after constructing a row, before any flush.
        kwargs.setdefault("status", "pending")
        kwargs.setdefault("attempt_count", 0)
        kwargs.setdefault("created_at", utcnow())
        super().__init__(**kwargs)
