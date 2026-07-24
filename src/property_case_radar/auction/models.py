"""Domain models for the auction (法拍屋) vertical.

DB INTEGRATION POINT: no shared PostgreSQL/ORM layer exists in this
repository yet (see CLAUDE.md: "no source code" at project start; the
planned ``database/models`` module per taiwan_real_estate_radar.md
section 十四 has not been built by either pipeline). These are plain
``@dataclass`` domain objects, not ORM models. They are deliberately
shaped to map 1:1 onto the tables suggested in section 十一:

    auction_cases          -> AuctionCase (minus rounds/documents/history,
                               which are their own tables)
    auction_rounds         -> AuctionRound
    auction_documents      -> AuctionDocument
    auction_status_history -> AuctionStatusEvent
    auction_subscriptions  -> AuctionSubscription

When a shared database layer lands, the expected integration shape is:
a SQLAlchemy (or equivalent) model per table above, plus a repository
that constructs/reads these dataclasses (or the ORM rows can be adapted
into these dataclasses at the boundary). Nothing outside this module
should need to change: scoring.py, state_machine.py, notifications.py,
and discord/commands.py all depend only on these dataclasses and on the
``AuctionRepository`` protocol in repository.py, not on any concrete
storage technology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaseType(str, Enum):
    """類型 (spec section 六 search example: 類型：住宅)."""

    RESIDENTIAL = "residential"  # 住宅
    STOREFRONT = "storefront"  # 店面
    LAND = "land"  # 土地
    OFFICE_FACTORY = "office_factory"  # 廠辦
    OTHER = "other"


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

    See state_machine.py for the allowed-transition table and the
    round-advance semantics ("流標 -> 轉下一拍").
    """

    ANNOUNCED = "announced"  # 新增公告
    CORRECTED = "corrected"  # 更正公告
    PRICE_CHANGED = "price_changed"  # 底價變更
    DATE_CHANGED = "date_changed"  # 拍賣日期變更
    FAILED = "failed"  # 流標 (本拍次)
    SUSPENDED = "suspended"  # 停拍
    WITHDRAWN = "withdrawn"  # 撤回
    AWARDED = "awarded"  # 拍定／得標


@dataclass
class AuctionRound:
    """One row of ``auction_rounds``: 拍次、底價、保證金、拍賣日期、結果."""

    round_number: int  # 拍次: 1, 2, 3, ... (4+ conventionally treated as 應買階段)
    floor_price_total: float  # 底價 (萬元)
    floor_unit_price: float  # 底價單價 (萬元／坪)
    auction_date: date | None = None  # 拍賣日期
    deposit: float | None = None  # 保證金 (萬元)
    result: RoundResult = RoundResult.PENDING
    winning_price: float | None = None  # 得標價格 (萬元), set when result == AWARDED

    def __post_init__(self) -> None:
        if self.round_number < 1:
            raise ValueError(f"round_number must be >= 1, got {self.round_number}")
        if self.floor_price_total <= 0:
            raise ValueError(f"floor_price_total must be positive, got {self.floor_price_total}")
        if self.floor_unit_price <= 0:
            raise ValueError(f"floor_unit_price must be positive, got {self.floor_unit_price}")
        if self.deposit is not None and self.deposit < 0:
            raise ValueError(f"deposit must not be negative, got {self.deposit}")
        if self.winning_price is not None and self.winning_price <= 0:
            raise ValueError(f"winning_price must be positive, got {self.winning_price}")

    @property
    def is_beyond_second_round(self) -> bool:
        """Used by /auction search's "拍次：二拍以上" filter."""
        return self.round_number >= 2


@dataclass
class AuctionDocument:
    """One row of ``auction_documents``: 公告網址／附件網址 per case."""

    doc_type: str  # e.g. "announcement", "attachment", "survey_map"
    url: str
    title: str = ""
    fetched_at: datetime = field(default_factory=_utcnow)
    content_hash: str | None = None  # for crawler-side de-duplication


@dataclass
class AuctionStatusEvent:
    """One row of ``auction_status_history``.

    ``previous_*``/``new_*`` are populated only for PRICE_CHANGED (底價
    變更) and DATE_CHANGED (拍賣日期變更) events, carrying the before/
    after payload so notifications.py can render what actually changed
    instead of just the status label. See state_machine.apply_transition.
    """

    to_status: AuctionStatus
    changed_at: datetime = field(default_factory=_utcnow)
    from_status: AuctionStatus | None = None
    round_number: int | None = None
    note: str = ""
    previous_floor_price_total: float | None = None
    new_floor_price_total: float | None = None
    previous_floor_unit_price: float | None = None
    new_floor_unit_price: float | None = None
    previous_auction_date: date | None = None
    new_auction_date: date | None = None


@dataclass
class AuctionFilter:
    """Shared predicate used by both ``/auction search`` and ``/auction subscribe``.

    Kept as one type so search and subscription-matching logic can never
    drift apart (spec section 六 search example and section 三/六 command
    list both describe the same filterable attributes).
    """

    city: str | None = None
    district: str | None = None
    case_type: CaseType | None = None
    floor_price_max: float | None = None  # 底價上限 (萬元)
    min_round: int | None = None  # 拍次: 二拍以上 -> min_round=2
    require_deliverable: bool | None = None  # 點交：是/否 (None = 不限)
    min_investment_score: float | None = None

    def matches(self, case: "AuctionCase") -> bool:
        if self.city and case.city != self.city:
            return False
        if self.district and case.district != self.district:
            return False
        if self.case_type and case.case_type != self.case_type:
            return False
        if self.floor_price_max is not None:
            current = case.current_round
            if current is None or current.floor_price_total > self.floor_price_max:
                return False
        if self.min_round is not None:
            if case.round_number is None or case.round_number < self.min_round:
                return False
        if self.require_deliverable is not None:
            if case.is_deliverable != self.require_deliverable:
                return False
        if self.min_investment_score is not None:
            if case.investment_score is None or case.investment_score < self.min_investment_score:
                return False
        return True


@dataclass
class AuctionSubscription:
    """One row of ``auction_subscriptions``."""

    subscription_id: str
    user_id: str
    filters: AuctionFilter
    channel_id: str | None = None
    active: bool = True
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class AuctionCase:
    """One row of ``auction_cases`` (rounds/documents/history are separate tables)."""

    case_id: str  # internal stable id, e.g. f"{court_name}:{case_number}"
    court_name: str  # 法院名稱
    case_number: str  # 案號
    first_seen_at: datetime  # 第一次發現時間
    updated_at: datetime  # 公告更新時間

    division: str = ""  # 股別
    case_type: CaseType = CaseType.OTHER
    city: str = ""
    district: str = ""
    address: str = ""

    announced_date: date | None = None  # 公告日期
    building_area_ping: float | None = None  # 建物坪數
    land_area_ping: float | None = None  # 土地坪數
    ownership_ratio: str = ""  # 權利範圍
    # Unknown/unparsed ownership MUST NOT default to FULL -- assuming full
    # ownership when the data is simply missing hides real persistence risk
    # (持分/共有 cases would silently score as if they were clean title).
    ownership_type: OwnershipType = OwnershipType.UNKNOWN

    occupancy_status: OccupancyStatus = OccupancyStatus.UNKNOWN
    occupancy_note: str = ""  # 占用情況

    # Natural-person data. MUST be masked before inclusion in any public
    # Discord notification -- see auction/masking.py. Safe to show in full
    # only in private channels (per discord 伺服器.txt: 房地案件-搜尋 /
    # 法拍案件-搜尋 are private) or to the requesting user via /auction detail.
    debtor: str = ""  # 債務人
    owner: str = ""  # 所有權人

    lease_status: str = ""  # 租賃狀態
    seizure_status: str = ""  # 查封狀態
    other_encumbrances: str = ""  # 他項權利
    building_use: str = ""  # 建物用途
    zoning: str = ""  # 土地使用分區
    has_unregistered_addition: bool = False  # 是否有增建

    announcement_url: str = ""  # 公告網址
    documents: list[AuctionDocument] = field(default_factory=list)

    rounds: list[AuctionRound] = field(default_factory=list)
    status: AuctionStatus = AuctionStatus.ANNOUNCED
    status_history: list[AuctionStatusEvent] = field(default_factory=list)

    risk_score: float | None = None  # 案件風險分數 (cached; see scoring.py)
    investment_score: float | None = None  # 案件投資分數 (cached; see scoring.py)

    # --- derived, read-only conveniences -----------------------------
    # The spec (section 七) lists 是否停拍/是否撤回/是否流標/是否拍定 as
    # suggested flat DB columns (useful for indexing/filtering). We derive
    # them from `status` instead of storing them separately, so there is
    # a single source of truth and they can never go stale relative to
    # status_history. A real DB row can still denormalize these into real
    # columns at write time if query performance requires it.

    @property
    def current_round(self) -> AuctionRound | None:
        return self.rounds[-1] if self.rounds else None

    @property
    def round_number(self) -> int | None:
        current = self.current_round
        return current.round_number if current else None

    @property
    def is_deliverable(self) -> bool | None:
        """點交：是/否, or None if unknown -- used by AuctionFilter and search/risk."""
        if self.occupancy_status in (OccupancyStatus.VACANT_DELIVERABLE, OccupancyStatus.OCCUPIED_DELIVERABLE):
            return True
        if self.occupancy_status in (OccupancyStatus.NOT_DELIVERABLE,):
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
    def winning_price(self) -> float | None:  # 得標價格
        current = self.current_round
        return current.winning_price if current else None
