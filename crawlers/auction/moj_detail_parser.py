"""Parser for real MOJ Administrative Enforcement Agency estate detail pages."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from crawlers.auction.parser import (
    AnnouncementKind,
    ParsedAnnouncement,
    ParsedDocumentLink,
)
from database.models.auction import CaseType, OccupancyStatus, OwnershipType

_CITY_DISTRICT = re.compile(
    r"^(?P<city>[^縣市]{1,5}[縣市])(?P<district>[^鄉鎮市區]{1,5}[鄉鎮市區])"
)
_NUMBER = re.compile(r"[\d,.]+")


class MojEstateDetailParser:
    """Translate the live ``/Detail/Estate`` DOM into the domain parser DTO."""

    def parse(self, raw_html: str, *, source_url: str = "") -> ParsedAnnouncement:
        soup = BeautifulSoup(raw_html, "html.parser")
        main = soup.select_one("#mainContent")
        if main is None:
            raise ValueError("no #mainContent found in MOJ estate detail page")

        texts = [" ".join(node.get_text(" ", strip=True).split()) for node in main.select("li")]
        case_number = _value_after(texts, ("案號：", "案號:"))
        if not case_number:
            raise ValueError("MOJ estate detail page has no case number")
        pdf_text = _embedded_pdf_text(soup)
        pdf_compact = " ".join(pdf_text.split())

        auction_raw = _value_after(texts, ("開標日：", "開標日:"))
        auction_date = None
        if auction_raw:
            try:
                auction_date = datetime.strptime(auction_raw.split()[0], "%Y/%m/%d").date()
            except ValueError as exc:
                raise ValueError(f"invalid MOJ auction date: {auction_raw!r}") from exc

        target_lists = []
        heading = next(
            (
                tag
                for tag in main.select("div.title_h3")
                if "拍賣標的價額及範圍" in tag.get_text(" ", strip=True)
            ),
            None,
        )
        if heading is not None:
            sibling = heading.find_next_sibling()
            while sibling is not None and sibling.name == "ul":
                target_lists.append(sibling)
                sibling = sibling.find_next_sibling()

        floor_total = 0
        building_ping = 0.0
        land_ping = 0.0
        addresses: list[str] = []
        for target in target_lists:
            items = [" ".join(li.get_text(" ", strip=True).split()) for li in target.select("li")]
            floor_total += _integer_after(items, ("底價:", "底價：")) or 0
            ping = _float_matching(items, r"面積[：:]\s*([\d,.]+)坪")
            address = _value_after(items, ("地址：", "地址:"))
            land_number = _value_after(items, ("地號：", "地號:"))
            if address:
                addresses.append(address)
                building_ping += ping or 0
            elif land_number:
                land_ping += ping or 0
            else:
                land_ping += ping or 0

        address = addresses[0] if addresses else ""
        location = _CITY_DISTRICT.match(address)
        metadata = parse_qs(urlparse(source_url).fragment)
        city = _normalize_city(
            location.group("city")
            if location
            else (metadata.get("radar_city") or [""])[0]
        )
        district = (
            location.group("district")
            if location
            else (metadata.get("radar_district") or [""])[0]
        )
        total_ping = building_ping + land_ping
        floor_unit = round(floor_total / total_ping) if floor_total and total_ping else None

        query = parse_qs(urlparse(source_url).query)
        round_number = _pdf_round_number(pdf_compact)
        if round_number is None:
            round_number = _positive_int((query.get("SALE") or [""])[0]) or 1

        kind = _announcement_kind(pdf_compact)
        announced_date = _roc_announcement_date(pdf_compact)
        debtor = _first_group(
            pdf_compact,
            (
                r"義務人[：:\s]*(.{1,60}?)所有",
                r"義務人[：:\s]*(.{1,60}?)如附",
            ),
        )
        occupancy_status, occupancy_note = _occupancy(pdf_compact)
        ownership_type = (
            OwnershipType.PARTIAL_SHARE
            if re.search(r"應有部分|持分", pdf_compact)
            else OwnershipType.UNKNOWN
        )
        ownership_ratio = _first_group(
            pdf_compact,
            (r"權利\s*範\s*圍[：:\s]*([0-9零一二三四五六七八九十百千萬分之／/ ]{2,30})",),
        )
        division = _first_group(pdf_compact, (r"[北桃新中彰嘉南高屏花宜]執([^\d\s]{1,4})",))
        deposit_twd = _largest_money(pdf_compact, "保證金")
        winning_price_twd = _largest_money(pdf_compact, "拍定價")
        zoning = _first_group(
            pdf_compact,
            (r"(?:都市計畫)?使用分區(?:及使用地類別)?為[「\"]?(.{1,50}?)[」\"。；;]",),
        )
        lease_status = "有租賃／承租資訊" if re.search(r"租約|承租人|租賃", pdf_compact) else ""
        seizure_status = "已查封" if "查封" in pdf_compact else ""
        other_encumbrances = "公告提及抵押權" if "抵押權" in pdf_compact else ""
        has_unregistered_addition = bool(
            re.search(r"未保存登記|增建部分|現場.{0,30}增建|另有.{0,30}增建", pdf_compact)
        )
        case_type = _case_type(pdf_compact, building_ping)

        documents: list[ParsedDocumentLink] = []
        for anchor in main.select('a[href*="/File/Download"]'):
            href = str(anchor.get("href", ""))
            documents.append(
                ParsedDocumentLink(
                    doc_type="announcement",
                    url=urljoin(source_url, href),
                    title=" ".join(anchor.get_text(" ", strip=True).split()),
                )
            )

        return ParsedAnnouncement(
            court_name="法務部行政執行署",
            case_number=case_number,
            kind=kind,
            division=division,
            case_type=case_type,
            city=city,
            district=district,
            address=address,
            announced_date=announced_date,
            auction_date=auction_date,
            round_number=round_number,
            floor_price_total_twd=floor_total or None,
            floor_unit_price_twd=floor_unit,
            deposit_twd=deposit_twd,
            winning_price_twd=winning_price_twd,
            building_area_ping=building_ping or None,
            land_area_ping=land_ping or None,
            ownership_ratio=ownership_ratio,
            ownership_type=ownership_type,
            occupancy_status=occupancy_status,
            occupancy_note=occupancy_note,
            debtor=debtor,
            owner=debtor,
            lease_status=lease_status,
            seizure_status=seizure_status,
            other_encumbrances=other_encumbrances,
            building_use=_building_use(pdf_compact),
            zoning=zoning,
            has_unregistered_addition=has_unregistered_addition,
            announcement_url=source_url,
            document_links=documents,
            source_hash=hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        )


def _value_after(values: list[str], prefixes: tuple[str, ...]) -> str:
    for value in values:
        for prefix in prefixes:
            if value.startswith(prefix):
                return value[len(prefix) :].strip()
    return ""


def _integer_after(values: list[str], prefixes: tuple[str, ...]) -> int | None:
    raw = _value_after(values, prefixes)
    match = _NUMBER.search(raw)
    return int(match.group().replace(",", "").split(".")[0]) if match else None


def _float_matching(values: list[str], pattern: str) -> float | None:
    for value in values:
        match = re.search(pattern, value)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_city(value: str) -> str:
    """Use the canonical MOI spelling so auction rows match market prices."""

    return value.replace("台", "臺")


def _embedded_pdf_text(soup: BeautifulSoup) -> str:
    node = soup.select_one("#radar-official-pdf-text")
    if node is None:
        return ""
    try:
        values = json.loads(html.unescape(node.get_text()))
    except (json.JSONDecodeError, TypeError):
        return ""
    return "\n".join(str(value) for value in values) if isinstance(values, list) else ""


def _first_group(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return " ".join(match.group(1).split()).strip("：:，,。 ")
    return ""


def _pdf_round_number(text: str) -> int | None:
    match = re.search(r"第\s*(\d+)\s*次拍賣", text)
    if match:
        return int(match.group(1))
    if "特別變賣" in text:
        return 4
    return None


def _announcement_kind(text: str) -> AnnouncementKind:
    heading = text[:600]
    if re.search(r"停止拍賣公告|公告停止拍賣|停拍公告", heading):
        return AnnouncementKind.SUSPENDED
    if re.search(r"撤回拍賣公告|公告撤回", heading):
        return AnnouncementKind.WITHDRAWN
    if re.search(r"拍定公告|得標公告", heading):
        return AnnouncementKind.AWARDED
    if re.search(r"流標公告|公告流標", heading):
        return AnnouncementKind.FAILED
    if "更正公告" in heading:
        if "底價" in heading:
            return AnnouncementKind.PRICE_CHANGE
        if re.search(r"日期|期日|開標日", heading):
            return AnnouncementKind.DATE_CHANGE
        return AnnouncementKind.CORRECTION
    return AnnouncementKind.NEW


def _roc_announcement_date(text: str) -> date | None:
    match = re.search(
        r"發文日期[：:\s]*(?:中華民國\s*)?(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        text,
    )
    if not match:
        return None
    return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))


def _money_values(text: str, label: str) -> list[int]:
    values: list[int] = []
    for amount, unit in re.findall(
        rf"{label}[^。\n]{{0,25}}?([\d,.]+)\s*(萬)?元",
        text,
    ):
        value = float(amount.replace(",", ""))
        values.append(round(value * (10_000 if unit else 1)))
    return values


def _largest_money(text: str, label: str) -> int | None:
    values = _money_values(text, label)
    return max(values) if values else None


def _occupancy(text: str) -> tuple[OccupancyStatus, str]:
    if re.search(r"第三人.{0,20}(?:占用|占有)", text):
        return OccupancyStatus.THIRD_PARTY_OCCUPIED, "公告記載第三人占用"
    if re.search(r"拍定後不點交|不予點交|不點交", text):
        return OccupancyStatus.NOT_DELIVERABLE, "公告記載不點交"
    if re.search(r"租約|承租人|租賃", text):
        return OccupancyStatus.LEASE_EXISTS, "公告記載租賃或承租資訊"
    if re.search(r"無人占用.{0,30}點交|拍定後點交", text):
        return OccupancyStatus.VACANT_DELIVERABLE, "公告記載點交"
    return OccupancyStatus.UNKNOWN, ""


def _case_type(text: str, building_ping: float) -> CaseType:
    if re.search(r"工廠|廠房|辦公室", text):
        return CaseType.OFFICE_FACTORY
    if re.search(r"店面|店舖|商場", text):
        return CaseType.STOREFRONT
    return CaseType.RESIDENTIAL if building_ping else CaseType.LAND


def _building_use(text: str) -> str:
    if re.search(r"工廠|廠房", text):
        return "廠房"
    if "辦公室" in text:
        return "辦公"
    if re.search(r"店面|店舖", text):
        return "店面"
    if re.search(r"住宅|住家", text):
        return "住宅"
    return ""
