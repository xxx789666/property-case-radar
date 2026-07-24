from pathlib import Path

import pytest

from property_case_radar.auction.models import CaseType, OccupancyStatus, OwnershipType
from property_case_radar.auction.parsers.base import AnnouncementKind
from property_case_radar.auction.parsers.court_announcement import CourtAnnouncementParser

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "src" / "property_case_radar" / "auction" / "fixtures"


@pytest.fixture
def parser() -> CourtAnnouncementParser:
    return CourtAnnouncementParser()


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_parse_new_announcement(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_load("announcement_new.html"), source_url="https://court.example.gov.tw/ann/12345")
    assert parsed.kind == AnnouncementKind.NEW
    assert parsed.court_name == "桃園地方法院"
    assert parsed.case_number == "115年度司執字第12345號"
    assert parsed.case_type == CaseType.RESIDENTIAL
    assert parsed.city == "桃園市"
    assert parsed.district == "中壢區"
    assert parsed.round_number == 2
    assert parsed.floor_price_total == 980.0
    assert parsed.floor_unit_price == 23.2
    assert parsed.ownership_type == OwnershipType.FULL
    assert parsed.occupancy_status == OccupancyStatus.VACANT_DELIVERABLE
    assert parsed.debtor == "王小明"
    assert parsed.announcement_url == "https://court.example.gov.tw/ann/12345"
    assert len(parsed.document_links) == 2
    assert parsed.document_links[0].doc_type == "announcement"
    assert parsed.source_hash  # non-empty


def test_parse_correction_announcement(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_load("announcement_correction.html"))
    assert parsed.kind == AnnouncementKind.CORRECTION
    assert parsed.case_number == "115年度司執字第12345號"
    assert parsed.address.endswith("之1號")


def test_parse_price_change_announcement(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_load("announcement_price_change.html"))
    assert parsed.kind == AnnouncementKind.PRICE_CHANGE
    assert parsed.round_number == 3
    assert parsed.floor_price_total == 784.0
    assert parsed.floor_unit_price == 18.5


def test_parse_date_change_announcement(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_load("announcement_date_change.html"))
    assert parsed.kind == AnnouncementKind.DATE_CHANGE
    assert parsed.auction_date is not None
    assert parsed.auction_date.isoformat() == "2026-09-29"


def test_parse_rejects_unknown_structure(parser: CourtAnnouncementParser) -> None:
    with pytest.raises(ValueError):
        parser.parse("<html><body>not an announcement</body></html>")


def test_parse_is_deterministic_hash(parser: CourtAnnouncementParser) -> None:
    html = _load("announcement_new.html")
    first = parser.parse(html)
    second = parser.parse(html)
    assert first.source_hash == second.source_hash


_MISSING_OWNERSHIP_HTML = """
<article class="court-announcement" data-kind="new">
  <dl>
    <dt>法院</dt><dd>測試地方法院</dd>
    <dt>案號</dt><dd>115年度司執字第99999號</dd>
    <dt>縣市</dt><dd>台北市</dd>
    <dt>行政區</dt><dd>大安區</dd>
  </dl>
</article>
"""

_UNRECOGNIZED_OWNERSHIP_HTML = """
<article class="court-announcement" data-kind="new">
  <dl>
    <dt>法院</dt><dd>測試地方法院</dd>
    <dt>案號</dt><dd>115年度司執字第88888號</dd>
    <dt>產權</dt><dd>不明</dd>
  </dl>
</article>
"""


def test_parse_missing_ownership_field_is_unknown_not_full(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_MISSING_OWNERSHIP_HTML)
    assert parsed.ownership_type == OwnershipType.UNKNOWN


def test_parse_unrecognized_ownership_text_is_unknown_not_full(parser: CourtAnnouncementParser) -> None:
    parsed = parser.parse(_UNRECOGNIZED_OWNERSHIP_HTML)
    assert parsed.ownership_type == OwnershipType.UNKNOWN
