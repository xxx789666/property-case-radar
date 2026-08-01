"""Court announcement parser (法院公告 -> structured data) for the auction pipeline.

COMPLIANCE (spec section 十六 "法拍公告"): 法拍資訊必須以法院正式公告為準
-- ``ParsedAnnouncement`` always retains ``announcement_url`` and
``document_links`` back to the original source, and ``source_hash`` so
the ingestion pipeline (apps/services/auction_pipeline.py) can detect
"nothing actually changed" vs. a real amendment without reprocessing.

This module's ``CourtAnnouncementParser`` targets a normalized
``<article class="court-announcement">`` label/value structure (see
``crawlers/auction/fixtures/*.html``), NOT any specific real court's
HTML. Real court announcement pages vary by 地方法院 and will need their
own field-mapping tables; prefer adding a new module (e.g.
``taipei_district_court.py``) that also returns ``ParsedAnnouncement``
rather than growing this one into a many-branched "guess the format"
parser -- selectors will need per-court maintenance as sites change
(section 十六: "網站改版後需維護選擇器與資料解析").

All monetary fields are whole TWD ints (matching
``scoring.auction_score`` / ``database.models.auction``'s unit
convention, and ``database.models.common.MarketPrice.average_unit_price_twd``)
-- the fixture HTML values are authored directly in TWD, not 萬元, so no
unit conversion happens in this module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from bs4 import BeautifulSoup

from database.models.auction import CaseType, OccupancyStatus, OwnershipType


class AnnouncementKind(str, Enum):
    """Which of the section 九 status events this announcement represents."""

    NEW = "new"  # 新增公告
    CORRECTION = "correction"  # 更正公告
    PRICE_CHANGE = "price_change"  # 底價變更
    DATE_CHANGE = "date_change"  # 拍賣日期變更
    FAILED = "failed"  # 流標 (optionally carries the next round's data too)
    SUSPENDED = "suspended"  # 停拍
    WITHDRAWN = "withdrawn"  # 撤回
    AWARDED = "awarded"  # 拍定／得標 (carries 得標價格)


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
    floor_price_total_twd: int | None = None  # 底價
    floor_unit_price_twd: int | None = None  # 底價單價 (元／坪)
    deposit_twd: int | None = None
    # 得標價格 -- only meaningful (and required by the ingestion pipeline)
    # when kind == AWARDED.
    winning_price_twd: int | None = None
    # Free-text note for FAILED/SUSPENDED/WITHDRAWN/status-change kinds --
    # see AuctionStatusHistory.note; never rendered to a public audience
    # (notifications/auction_notification.py).
    note: str = ""

    building_area_ping: float | None = None
    land_area_ping: float | None = None
    ownership_ratio: str = ""
    # MUST NOT default to FULL -- an unparsed/missing 產權 field means we
    # don't know, not that ownership is clean; see OwnershipType.
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


_CASE_TYPE_MAP = {
    "住宅": CaseType.RESIDENTIAL,
    "店面": CaseType.STOREFRONT,
    "土地": CaseType.LAND,
    "廠辦": CaseType.OFFICE_FACTORY,
}

_OWNERSHIP_MAP = {
    "完整所有權": OwnershipType.FULL,
    "持分": OwnershipType.PARTIAL_SHARE,
    "僅土地": OwnershipType.LAND_ONLY,
    "僅建物": OwnershipType.BUILDING_ONLY,
}

_OCCUPANCY_MAP = {
    "點交(無占用)": OccupancyStatus.VACANT_DELIVERABLE,
    "點交(有人居住)": OccupancyStatus.OCCUPIED_DELIVERABLE,
    "不點交": OccupancyStatus.NOT_DELIVERABLE,
    "有租賃": OccupancyStatus.LEASE_EXISTS,
    "第三人占用": OccupancyStatus.THIRD_PARTY_OCCUPIED,
}

_BOOL_MAP = {"是": True, "否": False}


def _text_of(dl_pairs: dict[str, str], label: str) -> str:
    return dl_pairs.get(label, "").strip()


def _parse_dl_pairs(soup: BeautifulSoup) -> dict[str, str]:
    pairs: dict[str, str] = {}
    dl = soup.find("dl")
    if dl is None:
        return pairs
    dts = dl.find_all("dt")
    dds = dl.find_all("dd")
    for dt, dd in zip(dts, dds):
        pairs[dt.get_text(strip=True)] = dd.get_text(strip=True)
    return pairs


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


class CourtAnnouncementParser:
    """Parses the fixture HTML format described in this module's docstring."""

    def parse(self, raw_html: str, *, source_url: str = "") -> ParsedAnnouncement:
        soup = BeautifulSoup(raw_html, "html.parser")
        article = soup.find("article", class_="court-announcement")
        if article is None:
            raise ValueError("no <article class='court-announcement'> found in input")

        kind_raw = article.get("data-kind", "new")
        try:
            kind = AnnouncementKind(kind_raw)
        except ValueError as exc:
            raise ValueError(f"unknown announcement kind: {kind_raw!r}") from exc

        pairs = _parse_dl_pairs(article)

        round_raw = _text_of(pairs, "拍次")
        floor_price_raw = _text_of(pairs, "底價")
        floor_unit_price_raw = _text_of(pairs, "底價單價")
        deposit_raw = _text_of(pairs, "保證金")
        building_area_raw = _text_of(pairs, "建物坪數")
        land_area_raw = _text_of(pairs, "土地坪數")
        winning_price_raw = _text_of(pairs, "得標價格")

        documents: list[ParsedDocumentLink] = []
        doc_list = article.find("ul", class_="documents")
        if doc_list is not None:
            for li in doc_list.find_all("li"):
                anchor = li.find("a")
                if anchor is None or not anchor.get("href"):
                    continue
                documents.append(
                    ParsedDocumentLink(
                        doc_type=li.get("data-type", "attachment"),
                        url=anchor["href"],
                        title=anchor.get_text(strip=True),
                    )
                )

        return ParsedAnnouncement(
            court_name=_text_of(pairs, "法院"),
            case_number=_text_of(pairs, "案號"),
            kind=kind,
            division=_text_of(pairs, "股別"),
            case_type=_CASE_TYPE_MAP.get(_text_of(pairs, "類型"), CaseType.OTHER),
            city=_text_of(pairs, "縣市"),
            district=_text_of(pairs, "行政區"),
            address=_text_of(pairs, "地址"),
            announced_date=_parse_date(_text_of(pairs, "公告日期")),
            auction_date=_parse_date(_text_of(pairs, "拍賣日期")),
            round_number=int(round_raw) if round_raw else None,
            floor_price_total_twd=int(floor_price_raw) if floor_price_raw else None,
            floor_unit_price_twd=int(floor_unit_price_raw) if floor_unit_price_raw else None,
            deposit_twd=int(deposit_raw) if deposit_raw else None,
            winning_price_twd=int(winning_price_raw) if winning_price_raw else None,
            note=_text_of(pairs, "備註"),
            building_area_ping=float(building_area_raw) if building_area_raw else None,
            land_area_ping=float(land_area_raw) if land_area_raw else None,
            ownership_ratio=_text_of(pairs, "權利範圍"),
            # Missing/unrecognized 產權 text must surface as UNKNOWN, never
            # silently assumed FULL -- see OwnershipType's docstring.
            ownership_type=_OWNERSHIP_MAP.get(_text_of(pairs, "產權"), OwnershipType.UNKNOWN),
            occupancy_status=_OCCUPANCY_MAP.get(_text_of(pairs, "點交"), OccupancyStatus.UNKNOWN),
            occupancy_note=_text_of(pairs, "占用情況"),
            debtor=_text_of(pairs, "債務人"),
            owner=_text_of(pairs, "所有權人"),
            lease_status=_text_of(pairs, "租賃狀態"),
            seizure_status=_text_of(pairs, "查封狀態"),
            other_encumbrances=_text_of(pairs, "他項權利"),
            building_use=_text_of(pairs, "建物用途"),
            zoning=_text_of(pairs, "土地使用分區"),
            has_unregistered_addition=_BOOL_MAP.get(_text_of(pairs, "增建"), False),
            announcement_url=source_url,
            document_links=documents,
            source_hash=hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        )


default_parser = CourtAnnouncementParser()
