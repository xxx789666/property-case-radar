"""Parser interface + intermediate representation for court announcements.

COMPLIANCE (taiwan_real_estate_radar.md section 十六 "法拍公告"):
法拍資訊必須以法院正式公告為準 -- ``ParsedAnnouncement`` always retains
``announcement_url`` and ``document_links`` back to the original source,
and ``source_hash`` so a crawler can detect "nothing actually changed"
vs. a real amendment without re-parsing.

``ParsedAnnouncement`` is intentionally a flat, source-of-truth-preserving
record, not yet an ``AuctionCase``. Turning a sequence of these into
case creation / state_machine transitions (新增公告 vs 更正公告 vs
底價變更 vs 拍賣日期變更) is an ingestion-layer concern that belongs to
whatever wires the crawler + parser + repository together in the
scheduler app (per CLAUDE.md's planned ``apps/scheduler``); that
ingestion glue does not exist yet and is out of scope for this vertical
slice, which stops at "raw HTML in, structured+validated data out."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol

from property_case_radar.auction.models import CaseType, OccupancyStatus, OwnershipType


class AnnouncementKind(str, Enum):
    """Which of the section 九 status events this announcement represents."""

    NEW = "new"  # 新增公告
    CORRECTION = "correction"  # 更正公告
    PRICE_CHANGE = "price_change"  # 底價變更
    DATE_CHANGE = "date_change"  # 拍賣日期變更


@dataclass
class ParsedDocumentLink:
    doc_type: str  # "announcement" | "attachment" | ...
    url: str
    title: str = ""


@dataclass
class ParsedAnnouncement:
    court_name: str
    case_number: str
    kind: AnnouncementKind

    division: str = ""  # 股別
    case_type: CaseType = CaseType.OTHER
    city: str = ""
    district: str = ""
    address: str = ""

    announced_date: date | None = None
    auction_date: date | None = None
    round_number: int | None = None
    floor_price_total: float | None = None  # 底價 (萬元)
    floor_unit_price: float | None = None  # 底價單價 (萬元／坪)
    deposit: float | None = None

    building_area_ping: float | None = None
    land_area_ping: float | None = None
    ownership_ratio: str = ""
    # MUST NOT default to FULL -- an unparsed/missing 產權 field means we
    # don't know, not that ownership is clean; see models.OwnershipType.
    ownership_type: OwnershipType = OwnershipType.UNKNOWN

    occupancy_status: OccupancyStatus = OccupancyStatus.UNKNOWN
    occupancy_note: str = ""

    debtor: str = ""
    owner: str = ""

    lease_status: str = ""
    seizure_status: str = ""
    other_encumbrances: str = ""
    building_use: str = ""
    zoning: str = ""
    has_unregistered_addition: bool = False

    announcement_url: str = ""
    document_links: list[ParsedDocumentLink] = field(default_factory=list)
    source_hash: str = ""


class AnnouncementParser(Protocol):
    def parse(self, raw_html: str, *, source_url: str = "") -> ParsedAnnouncement: ...
