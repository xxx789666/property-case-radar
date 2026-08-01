from pathlib import Path
from datetime import datetime, timezone
import json
import pytest

from apps.services.auction_pipeline import ingest_auction_announcement
from crawlers.auction.moj_detail_parser import MojEstateDetailParser, _announcement_kind
from crawlers.auction.parser import AnnouncementKind
from database.repositories.auction import AuctionRepository
from database.models.auction import CaseType


def test_real_moj_estate_detail_schema_is_parsed() -> None:
    html = (Path(__file__).parent / "fixtures" / "moj_estate_detail_sample.html").read_text(
        encoding="utf-8"
    )
    parsed = MojEstateDetailParser().parse(
        html,
        source_url="https://www.tpkonsale.moj.gov.tw/Detail/Estate?NO=x&SALE=2",
    )

    assert parsed.court_name == "法務部行政執行署"
    assert parsed.case_number == "1010100050597"
    assert parsed.auction_date.isoformat() == "2026-08-04"
    assert parsed.round_number == 2
    assert parsed.floor_price_total_twd == 1_720_000
    assert parsed.floor_unit_price_twd == 6_512
    assert parsed.building_area_ping == 60.25497
    assert parsed.land_area_ping == 203.885
    assert parsed.city == "桃園市"
    assert parsed.district == "中壢區"
    assert parsed.case_type == CaseType.RESIDENTIAL
    assert parsed.document_links[0].url == (
        "https://www.tpkonsale.moj.gov.tw/File/Download?NAME=notice.pdf"
    )


def test_city_uses_canonical_tai_character() -> None:
    html = (Path(__file__).parent / "fixtures" / "moj_estate_detail_sample.html").read_text(
        encoding="utf-8"
    ).replace("桃園市", "台北市")

    parsed = MojEstateDetailParser().parse(
        html,
        source_url="https://www.tpkonsale.moj.gov.tw/Detail/Estate?NO=x&SALE=2",
    )

    assert parsed.city == "臺北市"


def test_parsed_moj_detail_enters_existing_auction_pipeline(session_factory) -> None:
    html = (Path(__file__).parent / "fixtures" / "moj_estate_detail_sample.html").read_text(
        encoding="utf-8"
    )
    parsed = MojEstateDetailParser().parse(
        html,
        source_url="https://www.tpkonsale.moj.gov.tw/Detail/Estate?NO=x&SALE=2",
    )
    with session_factory() as session:
        outcome = ingest_auction_announcement(
            parsed,
            AuctionRepository(session),
            fetched_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
        session.commit()

        assert outcome.created is True
        assert outcome.case.current_round.round_number == 2
        assert outcome.case.current_round.floor_price_total_twd == 1_720_000


def test_capture_metadata_supplies_location_for_land_without_address() -> None:
    html = """
    <div id="mainContent">
      <ul><li>案號：1140100133653</li><li>開標日：2026/08/04 15:00:00</li></ul>
      <div class="title_h3">拍賣標的價額及範圍</div>
      <ul><li>底價:288,000</li><li>面積：15.4517坪</li>
          <li>地號：190216440000</li><li>地址：</li></ul>
    </div>
    """
    parsed = MojEstateDetailParser().parse(
        html,
        source_url=(
            "https://www.tpkonsale.moj.gov.tw/Detail/Estate?SALE=1"
            "#radar_city=%E6%A1%83%E5%9C%92%E5%B8%82&"
            "radar_district=%E9%BE%8D%E6%BD%AD%E5%8D%80"
        ),
    )
    assert parsed.city == "桃園市"
    assert parsed.district == "龍潭區"
    assert parsed.case_type == CaseType.LAND


def test_embedded_official_pdf_supplies_round_risk_and_identity_fields() -> None:
    payload = """
    法務部行政執行署桃園分署公告（第 2 次拍賣）
    發文日期：中華民國 115 年 7 月 14 日
    發文字號：桃執戊 101 年贈稅執特專字第 00050597 號
    義務人吳徐員妹所有如附表所示不動產。
    保證金 35 萬元。
    本件係拍賣建物應有部分，拍定後不點交。
    承租人提出租約，使用分區為住宅區。
    """
    html = f"""
    <div id="mainContent">
      <ul><li>案號：1010100050597</li><li>開標日：2026/08/04 15:00:00</li></ul>
      <div class="title_h3">拍賣標的價額及範圍</div>
      <ul><li>底價:1,720,000</li><li>面積：60坪</li>
          <li>地號：</li><li>地址：桃園市中壢區長沙路19號</li></ul>
    </div>
    <script id="radar-official-pdf-text" type="application/json">
      {json.dumps([payload], ensure_ascii=False)}
    </script>
    """
    parsed = MojEstateDetailParser().parse(
        html,
        source_url="https://www.tpkonsale.moj.gov.tw/Detail/Estate?SALE=1",
    )
    assert parsed.round_number == 2
    assert parsed.announced_date.isoformat() == "2026-07-14"
    assert parsed.division == "戊"
    assert parsed.deposit_twd == 350_000
    assert parsed.debtor == "吳徐員妹"
    assert parsed.owner == "吳徐員妹"
    assert parsed.ownership_type.value == "partial_share"
    assert parsed.occupancy_status.value == "not_deliverable"
    assert parsed.lease_status
    assert parsed.zoning == "住宅區"


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("法務部行政執行署公告停止拍賣", AnnouncementKind.SUSPENDED),
        ("法務部行政執行署撤回拍賣公告", AnnouncementKind.WITHDRAWN),
        ("法務部行政執行署拍定公告", AnnouncementKind.AWARDED),
        ("法務部行政執行署流標公告", AnnouncementKind.FAILED),
        ("法務部行政執行署更正公告：底價修正", AnnouncementKind.PRICE_CHANGE),
        ("法務部行政執行署更正公告：開標日期修正", AnnouncementKind.DATE_CHANGE),
        ("法務部行政執行署更正公告", AnnouncementKind.CORRECTION),
    ],
)
def test_official_status_headings_are_classified(
    heading: str, expected: AnnouncementKind
) -> None:
    assert _announcement_kind(heading) == expected
