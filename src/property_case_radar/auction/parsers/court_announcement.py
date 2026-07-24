"""Reference parser for a court announcement page.

This parser targets a normalized ``<article class="court-announcement">``
label/value structure (see ``auction/fixtures/*.html``), NOT any specific
real court's HTML. Real court announcement pages vary by 地方法院 and
will need their own field-mapping tables; this module exists to prove
out the ``AnnouncementParser`` contract end-to-end against fixtures
without performing any live crawl (spec section 十六: avoid unnecessary
live crawling; this round ships fixture-backed testing only).

When wiring a real court's page, prefer adding a new module (e.g.
``taipei_district_court.py``) that also returns ``ParsedAnnouncement``
rather than growing this one into a many-branched "guess the format"
parser -- selectors will need per-court maintenance as sites change
(section 十六: "網站改版後需維護選擇器與資料解析").
"""

from __future__ import annotations

import hashlib
from datetime import date

from bs4 import BeautifulSoup

from property_case_radar.auction.models import CaseType, OccupancyStatus, OwnershipType
from property_case_radar.auction.parsers.base import (
    AnnouncementKind,
    AnnouncementParser,
    ParsedAnnouncement,
    ParsedDocumentLink,
)

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

        parsed = ParsedAnnouncement(
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
            floor_price_total=float(floor_price_raw) if floor_price_raw else None,
            floor_unit_price=float(floor_unit_price_raw) if floor_unit_price_raw else None,
            deposit=float(deposit_raw) if deposit_raw else None,
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
        return parsed


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


# Module-level singleton matching the AnnouncementParser Protocol shape,
# for convenient `from ... import default_parser`.
default_parser: AnnouncementParser = CourtAnnouncementParser()
