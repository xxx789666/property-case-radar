"""SQLAlchemy models for the auction (法拍屋) vertical.

Independent per pipeline (CLAUDE.md): separate tables, separate status
machine (see apps/services/auction_pipeline.py and the status-transition
helper below), separate risk fields from the sale pipeline's `Property`.
Shares only `MarketPrice` (database/models/common.py, whole-TWD unit
price keyed by city+district+building_type -- see scoring/auction_score.py
for how auction floor prices are compared against it in the same units)
and, for logging, an optional link from `NotificationLog.auction_case_id`.

All monetary columns are whole TWD (`*_twd`, matching `Property.total_price_twd`
/ `MarketPrice.average_unit_price_twd`), not "萬元" -- this was a real unit
mismatch in the standalone auction prototype (它以萬元/坪 為單位) that would
have silently produced discount rates off by a factor of 10,000 once wired
to the real shared `market_prices` table. Fixed here by construction: there
is only one unit convention in this schema.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from database.models.base import Base, TimestampMixin, utcnow


class CaseType(str, Enum):
    """類型 (spec section 六 search example: 類型：住宅)."""

    RESIDENTIAL = "residential"  # 住宅
    STOREFRONT = "storefront"  # 店面
    LAND = "land"  # 土地
    OFFICE_FACTORY = "office_factory"  # 廠辦
    OTHER = "other"


# Canonical CaseType <-> Chinese label mapping, shared by notification
# rendering (notifications/auction_notification.py) and the
# MarketPrice.building_type lookup key (apps/services/auction_pipeline.py)
# so the two never drift into two different label sets for the same enum.
CASE_TYPE_LABELS: dict["CaseType", str] = {
    CaseType.RESIDENTIAL: "住宅",
    CaseType.STOREFRONT: "店面",
    CaseType.LAND: "土地",
    CaseType.OFFICE_FACTORY: "廠辦",
    CaseType.OTHER: "其他",
}


class OwnershipType(str, Enum):
    """產權完整程度, feeds the 產權完整 20-分 scoring component (spec section 八)."""

    FULL = "full"  # 完整所有權
    PARTIAL_SHARE = "partial_share"  # 持分拍賣
    LAND_ONLY = "land_only"  # 只有土地
    BUILDING_ONLY = "building_only"  # 只有建物
    UNKNOWN = "unknown"  # not yet parsed/confirmed -- MUST NOT default to FULL


class OccupancyStatus(str, Enum):
    """點交與占用狀態, feeds the 點交狀態 20-分 scoring component (spec section 八)."""

    VACANT_DELIVERABLE = "vacant_deliverable"  # 點交、無占用
    OCCUPIED_DELIVERABLE = "occupied_deliverable"  # 點交但有人居住
    NOT_DELIVERABLE = "not_deliverable"  # 不點交
    LEASE_EXISTS = "lease_exists"  # 有租賃關係，需檢查租約
    THIRD_PARTY_OCCUPIED = "third_party_occupied"  # 第三人占用，高風險
    UNKNOWN = "unknown"


class RoundResult(str, Enum):
    PENDING = "pending"
    FAILED = "failed"  # 流標
    WITHDRAWN = "withdrawn"  # 撤回
    SUSPENDED = "suspended"  # 停拍
    AWARDED = "awarded"  # 拍定／得標


class AuctionStatus(str, Enum):
    """Case-level status per taiwan_real_estate_radar.md section 九.

    See apps/services/auction_pipeline.py's ``apply_status_transition`` for
    the allowed-transition table and round-advance semantics.
    """

    ANNOUNCED = "announced"  # 新增公告
    CORRECTED = "corrected"  # 更正公告
    PRICE_CHANGED = "price_changed"  # 底價變更
    DATE_CHANGED = "date_changed"  # 拍賣日期變更
    FAILED = "failed"  # 流標 (本拍次)
    SUSPENDED = "suspended"  # 停拍
    WITHDRAWN = "withdrawn"  # 撤回
    AWARDED = "awarded"  # 拍定／得標


# States in which the case is still an active, unresolved announcement.
ACTIVE_STATUSES = frozenset(
    {AuctionStatus.ANNOUNCED, AuctionStatus.CORRECTED, AuctionStatus.PRICE_CHANGED, AuctionStatus.DATE_CHANGED}
)

# Terminal in v1 -- see apply_status_transition's module docstring for why
# SUSPENDED has no resume path yet.
TERMINAL_STATUSES = frozenset({AuctionStatus.WITHDRAWN, AuctionStatus.AWARDED, AuctionStatus.SUSPENDED})


def _enum_column(enum_cls: type[Enum], *, length: int, default: Enum | None = None, nullable: bool = False):
    return mapped_column(
        SAEnum(enum_cls, native_enum=False, length=length, validate_strings=True),
        default=default,
        nullable=nullable,
    )


class AuctionCase(Base, TimestampMixin):
    """One row of ``auction_cases``. ``updated_at`` doubles as 公告更新時間."""

    __tablename__ = "auction_cases"
    __table_args__ = (UniqueConstraint("court_name", "case_number", name="uq_auction_case_court_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Overrides TimestampMixin's `updated_at` to drop its `onupdate=utcnow`:
    # here `updated_at` IS "公告更新時間", the anchor
    # apps.services.auction_pipeline.apply_status_transition's append-
    # only-in-time invariant is checked against. It must change only via
    # an explicit assignment tied to a real announcement/status event --
    # never implicitly, e.g. because some unrelated column (like a cached
    # score) got updated in the same flush and SQLAlchemy's generic
    # "stamp now on any UPDATE" behavior fired for this column too.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    court_name: Mapped[str] = mapped_column(String(64), index=True)  # 法院名稱
    case_number: Mapped[str] = mapped_column(String(128), index=True)  # 案號
    division: Mapped[str] = mapped_column(String(32), default="")  # 股別
    case_type: Mapped[CaseType] = _enum_column(CaseType, length=32, default=CaseType.OTHER)
    city: Mapped[str] = mapped_column(String(32), default="", index=True)
    district: Mapped[str] = mapped_column(String(32), default="", index=True)
    address: Mapped[str | None] = mapped_column(String(255))

    announced_date: Mapped[date | None] = mapped_column(Date)  # 公告日期
    building_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 建物坪數
    land_area_ping: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # 土地坪數
    ownership_ratio: Mapped[str] = mapped_column(String(64), default="")  # 權利範圍
    # Unknown/unparsed ownership MUST NOT default to FULL -- assuming full
    # ownership when the data is simply missing hides real persistence risk.
    ownership_type: Mapped[OwnershipType] = _enum_column(OwnershipType, length=32, default=OwnershipType.UNKNOWN)

    occupancy_status: Mapped[OccupancyStatus] = _enum_column(
        OccupancyStatus, length=32, default=OccupancyStatus.UNKNOWN
    )
    occupancy_note: Mapped[str] = mapped_column(String(255), default="")  # 占用情況

    # Natural-person data. MUST be masked before inclusion in any public
    # Discord notification -- see notifications/auction_masking.py. Safe to
    # show in full only in the private search channel (per discord
    # 伺服器.txt: 法拍案件-搜尋) or a user's own /auction detail lookup.
    debtor: Mapped[str] = mapped_column(String(255), default="")  # 債務人
    owner: Mapped[str] = mapped_column(String(255), default="")  # 所有權人

    lease_status: Mapped[str] = mapped_column(String(128), default="")  # 租賃狀態
    seizure_status: Mapped[str] = mapped_column(String(128), default="")  # 查封狀態
    other_encumbrances: Mapped[str] = mapped_column(String(255), default="")  # 他項權利
    building_use: Mapped[str] = mapped_column(String(64), default="")  # 建物用途
    zoning: Mapped[str] = mapped_column(String(64), default="")  # 土地使用分區
    has_unregistered_addition: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否有增建

    announcement_url: Mapped[str] = mapped_column(Text, default="")  # 公告網址

    status: Mapped[AuctionStatus] = _enum_column(AuctionStatus, length=32, default=AuctionStatus.ANNOUNCED)

    surface_discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))  # cached; see scoring
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # 案件風險分數 (0-100)
    investment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # 案件投資分數 (0-100)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)  # 第一次發現時間

    rounds: Mapped[list["AuctionRound"]] = relationship(
        back_populates="case", order_by="AuctionRound.round_number", cascade="all, delete-orphan"
    )
    documents: Mapped[list["AuctionDocument"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    status_history: Mapped[list["AuctionStatusHistory"]] = relationship(
        back_populates="case", order_by="AuctionStatusHistory.changed_at", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs: object) -> None:
        # SQLAlchemy's declarative-generated __init__ does NOT apply
        # mapped_column(default=...) to the Python object -- that default
        # is only used by the ORM at flush/INSERT time. Business logic
        # (apps.services.auction_pipeline) and tests routinely construct
        # an AuctionCase and read attributes like `.status` or
        # `.ownership_type` before it's ever added to a session, so
        # without this override those would silently read back as `None`
        # instead of ANNOUNCED/UNKNOWN/etc, breaking every enum-equality
        # check in scoring/state-machine code (and producing a confusing
        # AttributeError deep inside error-message formatting rather than
        # a clear failure at the actual bug site).
        kwargs.setdefault("updated_at", utcnow())
        kwargs.setdefault("first_seen_at", utcnow())
        kwargs.setdefault("division", "")
        kwargs.setdefault("case_type", CaseType.OTHER)
        kwargs.setdefault("city", "")
        kwargs.setdefault("district", "")
        kwargs.setdefault("ownership_ratio", "")
        kwargs.setdefault("ownership_type", OwnershipType.UNKNOWN)
        kwargs.setdefault("occupancy_status", OccupancyStatus.UNKNOWN)
        kwargs.setdefault("occupancy_note", "")
        kwargs.setdefault("debtor", "")
        kwargs.setdefault("owner", "")
        kwargs.setdefault("lease_status", "")
        kwargs.setdefault("seizure_status", "")
        kwargs.setdefault("other_encumbrances", "")
        kwargs.setdefault("building_use", "")
        kwargs.setdefault("zoning", "")
        kwargs.setdefault("has_unregistered_addition", False)
        kwargs.setdefault("announcement_url", "")
        kwargs.setdefault("status", AuctionStatus.ANNOUNCED)
        super().__init__(**kwargs)

    # --- derived, read-only conveniences -----------------------------
    # The spec (section 七) lists 是否停拍/是否撤回/是否流標/是否拍定 as
    # suggested flat DB columns. We derive them from `status` instead of
    # storing them separately so there is a single source of truth.

    @property
    def current_round(self) -> "AuctionRound | None":
        return self.rounds[-1] if self.rounds else None

    @property
    def round_number(self) -> int | None:
        current = self.current_round
        return current.round_number if current else None

    @property
    def is_deliverable(self) -> bool | None:
        """點交：是/否, or None if unknown."""
        if self.occupancy_status in (OccupancyStatus.VACANT_DELIVERABLE, OccupancyStatus.OCCUPIED_DELIVERABLE):
            return True
        if self.occupancy_status == OccupancyStatus.NOT_DELIVERABLE:
            return False
        return None

    @property
    def is_suspended(self) -> bool:  # 是否停拍
        return self.status == AuctionStatus.SUSPENDED

    @property
    def is_withdrawn(self) -> bool:  # 是否撤回
        return self.status == AuctionStatus.WITHDRAWN

    @property
    def is_failed(self) -> bool:  # 是否流標 (current status, not historical)
        return self.status == AuctionStatus.FAILED

    @property
    def is_awarded(self) -> bool:  # 是否拍定
        return self.status == AuctionStatus.AWARDED

    @property
    def is_partial_share(self) -> bool:  # 是否持分
        return self.ownership_type == OwnershipType.PARTIAL_SHARE

    @property
    def winning_price_twd(self) -> int | None:  # 得標價格
        current = self.current_round
        return current.winning_price_twd if current else None


class AuctionRound(Base):
    """One row of ``auction_rounds``: 拍次、底價、保證金、拍賣日期、結果."""

    __tablename__ = "auction_rounds"
    __table_args__ = (UniqueConstraint("case_id", "round_number", name="uq_auction_round_case_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("auction_cases.id", ondelete="CASCADE"), index=True)
    round_number: Mapped[int] = mapped_column()  # 拍次: 1, 2, 3, ... (4+ = 應買階段)
    floor_price_total_twd: Mapped[int] = mapped_column(BigInteger)  # 底價 (整數台幣)
    floor_unit_price_twd: Mapped[int] = mapped_column(BigInteger)  # 底價單價 (元／坪)
    auction_date: Mapped[date | None] = mapped_column(Date)  # 拍賣日期
    deposit_twd: Mapped[int | None] = mapped_column(BigInteger)  # 保證金
    result: Mapped[RoundResult] = _enum_column(RoundResult, length=16, default=RoundResult.PENDING)
    winning_price_twd: Mapped[int | None] = mapped_column(BigInteger)  # 得標價格

    case: Mapped[AuctionCase] = relationship(back_populates="rounds")

    def __init__(self, **kwargs: object) -> None:
        # See AuctionCase.__init__ for why this is necessary: `result`
        # defaulting to PENDING must hold immediately at construction --
        # apply_status_transition's round-advance logic checks
        # `case.current_round.result == RoundResult.PENDING` on rounds
        # that may never have been through a session flush.
        kwargs.setdefault("result", RoundResult.PENDING)
        super().__init__(**kwargs)

    @validates("round_number")
    def _validate_round_number(self, key: str, value: int) -> int:
        if value < 1:
            raise ValueError(f"round_number must be >= 1, got {value}")
        return value

    @validates("floor_price_total_twd")
    def _validate_floor_price_total(self, key: str, value: int) -> int:
        if value is not None and value <= 0:
            raise ValueError(f"floor_price_total_twd must be positive, got {value}")
        return value

    @validates("floor_unit_price_twd")
    def _validate_floor_unit_price(self, key: str, value: int) -> int:
        if value is not None and value <= 0:
            raise ValueError(f"floor_unit_price_twd must be positive, got {value}")
        return value

    @validates("deposit_twd")
    def _validate_deposit(self, key: str, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError(f"deposit_twd must not be negative, got {value}")
        return value

    @validates("winning_price_twd")
    def _validate_winning_price(self, key: str, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError(f"winning_price_twd must be positive, got {value}")
        return value

    @property
    def is_beyond_second_round(self) -> bool:
        """Used by /auction search's "拍次：二拍以上" filter."""
        return self.round_number >= 2


class AuctionDocument(Base):
    """One row of ``auction_documents``: 公告網址／附件網址 per case.

    ``content_hash`` is stamped with the *announcement page's* content
    hash (``crawlers.auction.parser.ParsedAnnouncement.source_hash``) for
    every document extracted from that announcement, not a hash of the
    document's own bytes -- apps.services.auction_pipeline uses membership
    in a case's existing ``content_hash`` values to detect "we've already
    ingested this exact announcement before" (a case accumulates several
    *different* announcements over its lifetime -- 新公告/更正/底價變更/
    ..., each with its own hash -- so a single last-seen-hash field on
    ``AuctionCase`` can't tell "seen this one before" from "this is a
    different announcement than the most recent one").
    """

    __tablename__ = "auction_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("auction_cases.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32))  # "announcement" | "attachment" | ...
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(255), default="")
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # crawler-side de-duplication
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[AuctionCase] = relationship(back_populates="documents")


class AuctionStatusHistory(Base):
    """One row of ``auction_status_history``.

    ``previous_*``/``new_*`` are populated only for PRICE_CHANGED (底價
    變更) and DATE_CHANGED (拍賣日期變更) events, carrying the before/
    after payload so notifications can render what actually changed.
    """

    __tablename__ = "auction_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("auction_cases.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[AuctionStatus | None] = _enum_column(AuctionStatus, length=32, nullable=True)
    to_status: Mapped[AuctionStatus] = _enum_column(AuctionStatus, length=32)
    round_number: Mapped[int | None] = mapped_column()
    note: Mapped[str] = mapped_column(String(500), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    previous_floor_price_total_twd: Mapped[int | None] = mapped_column(BigInteger)
    new_floor_price_total_twd: Mapped[int | None] = mapped_column(BigInteger)
    previous_floor_unit_price_twd: Mapped[int | None] = mapped_column(BigInteger)
    new_floor_unit_price_twd: Mapped[int | None] = mapped_column(BigInteger)
    previous_auction_date: Mapped[date | None] = mapped_column(Date)
    new_auction_date: Mapped[date | None] = mapped_column(Date)

    case: Mapped[AuctionCase] = relationship(back_populates="status_history")


class AuctionSubscription(Base, TimestampMixin):
    """One row of ``auction_subscriptions``."""

    __tablename__ = "auction_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    city: Mapped[str | None] = mapped_column(String(32))
    district: Mapped[str | None] = mapped_column(String(32))
    case_type: Mapped[CaseType | None] = _enum_column(CaseType, length=32, nullable=True)
    max_floor_price_twd: Mapped[int | None] = mapped_column(BigInteger)  # 底價上限
    min_round: Mapped[int | None] = mapped_column()  # 拍次: 二拍以上 -> min_round=2
    require_deliverable: Mapped[bool | None] = mapped_column(Boolean)  # 點交：是/否 (None = 不限)
    min_investment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __init__(self, **kwargs: object) -> None:
        # See AuctionCase.__init__: `active` must default to True even
        # for a not-yet-flushed instance, since
        # database.repositories.auction.AuctionSubscriptionRepository and
        # the /auction subscribe command logic branch on it immediately.
        kwargs.setdefault("active", True)
        super().__init__(**kwargs)
