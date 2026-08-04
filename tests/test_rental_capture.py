import json
import importlib.util
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from unittest.mock import AsyncMock

from crawlers.rental.base import RentalListing
from crawlers.rental.captured_source import CapturedRentalCrawler, result_path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "capture_rental_results.py"
SPEC = importlib.util.spec_from_file_location("capture_rental_results", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_parse_public_591_rental_card() -> None:
    item = capture.parse_card(
        "桃園市",
        {
            "source_property_id": "12345678",
            "url": "https://rent.591.com.tw/12345678",
            "title": "近車站整層住家",
            "text": "整層住家 2房1廳 25坪 3F/10F 可開伙 有電梯 25,000元/月",
            "location": "中壢區-中正路",
            "features": ["可開伙", "有電梯"],
        },
    )
    assert item is not None
    assert item["district"] == "中壢區"
    assert item["address"] == "中正路"
    assert item["monthly_rent_twd"] == 25_000
    assert item["area_ping"] == "25"
    assert item["rent_per_ping_twd"] == 1_000


def test_result_path_reads_reported_capture(tmp_path: Path) -> None:
    output = tmp_path / "rental.json"
    output.write_text(json.dumps({"listings": []}), encoding="utf-8")
    stdout = json.dumps({"status": "ok", "output": str(output)})
    assert result_path(stdout) == output


def test_captured_rental_defaults_to_three_pages(tmp_path: Path) -> None:
    crawler = CapturedRentalCrawler("capture.py", output_dir=tmp_path)
    assert crawler.max_pages == 3
    assert crawler.other_max_pages == 10
    assert crawler.focus_max_pages == 10
    assert crawler.json_retention_days == 30


def test_focus_districts_are_deduplicated_and_encoded(tmp_path: Path) -> None:
    crawler = CapturedRentalCrawler("capture.py", output_dir=tmp_path)
    crawler.set_focus_districts(
        [("桃園市", "中壢區"), ("桃園市", "中壢區")]
    )
    assert crawler.focus_districts == ("桃園市|中壢區",)


def test_result_url_can_request_591_other_rentals() -> None:
    query = parse_qs(urlparse(capture.result_url(6, 2, 24)).query)
    assert query == {
        "region": ["6"],
        "kind": ["24"],
        "order": ["posttime_desc"],
        "page": ["2"],
    }


def test_capture_includes_general_and_other_rental_categories() -> None:
    assert capture.RENTAL_KINDS == (0, 24)
    assert capture.FOCUS_RENTAL_KINDS == (1, 2, 3, 4, 8, 24)
    assert capture.BUSINESS_RENTAL_KINDS == (5, 6, 12, 7)


def test_parse_public_591_business_rental_card() -> None:
    item = capture.parse_business_card(
        "桃園市",
        5,
        {
            "source_property_id": "21704115",
            "url": "https://business.591.com.tw/rent/21704115",
            "title": "過嶺生活圈近國中店墅",
            "text": "79.55坪 1F/4F 住商用 可登記 隨時可遷入 48,000元/月",
            "location": "中壢區-松仁路",
        },
    )
    assert item is not None
    assert item["source"] == "591-business"
    assert item["district"] == "中壢區"
    assert item["monthly_rent_twd"] == 48_000
    assert item["area_ping"] == "79.55"
    assert item["rental_type"] == "其他"
    assert "店面" in str(item["features"])


def test_business_area_uses_structured_card_value_instead_of_title_numbers() -> None:
    item = capture.parse_business_card(
        "臺中市",
        7,
        {
            "source_property_id": "21183435",
            "url": "https://business.591.com.tw/rent/21183435",
            "title": "廠房建600,900,1200,2000坪",
            "text": "廠房建600,900,1200,2000坪 900坪 整棟/3F 300,000元/月",
            "area": "900坪整棟/3F",
            "location": "大里區-仁化路",
        },
    )
    assert item is not None
    assert item["area_ping"] == "900"
    assert item["rent_per_ping_twd"] == 333


def test_rental_listing_rejects_area_outside_database_precision() -> None:
    with pytest.raises(ValueError, match=r"Numeric\(10, 2\)"):
        RentalListing(
            source="591-business",
            source_property_id="21183435",
            url="https://business.591.com.tw/rent/21183435",
            title="invalid area",
            city="臺中市",
            district="大里區",
            monthly_rent_twd=300_000,
            rent_per_ping_twd=1,
            area_ping=Decimal("60090012002000"),
        )


def test_parse_public_591_live_work_rental_card() -> None:
    item = capture.parse_business_card(
        "桃園市",
        12,
        {
            "url": "https://rent.591.com.tw/rent-detail-21404973.html",
            "title": "華泰商城旁美住辦出租",
            "text": "165坪 2F/10F 住辦 115,626元/月",
            "location": "大園區-中山南路一段",
        },
    )
    assert item is not None
    assert item["source"] == "591-rent"
    assert item["source_property_id"] == "21404973"
    assert item["url"] == "https://rent.591.com.tw/rent-detail-21404973.html"
    assert item["rental_type"] == "其他"
    assert "住辦" in str(item["features"])


def test_business_result_url_requests_public_rentals() -> None:
    query = parse_qs(
        urlparse(capture.business_result_url(6, 2, 5, 67)).query
    )
    assert query == {
        "region": ["6"],
        "kind": ["5"],
        "type": ["1"],
        "order": ["posttime_desc"],
        "page": ["2"],
        "section": ["67"],
    }


def test_business_detail_url_keeps_business_host() -> None:
    assert capture.normalize_detail_url(
        "https://business.591.com.tw/rent/21704115?from=search"
    ) == "https://business.591.com.tw/rent/21704115"


async def test_retry_failed_cities_keeps_only_unresolved_cities(
    tmp_path: Path,
) -> None:
    crawler = CapturedRentalCrawler("capture.py", output_dir=tmp_path)
    policy = {
        "public_pages_only": True,
        "login_used": False,
        "access_control_bypassed": False,
        "rental_kinds": [0, 24],
        "business_rental_kinds": [5, 6, 12, 7],
    }
    crawler._capture_payload = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "policy": policy,
                "cities": [{"city": "桃園市", "status": "ok"}],
                "listings": [],
            },
            {
                "policy": policy,
                "cities": [
                    {"city": "新竹縣", "status": "error", "error": "timeout"}
                ],
                "listings": [],
            },
        ]
    )

    listings = await crawler.retry_failed_cities(("桃園市", "新竹縣"))

    assert listings == []
    assert crawler.last_failed_cities == ("新竹縣",)
    assert crawler.last_health_error == "591 租屋部分抓取失敗／待重試：新竹縣"
    assert crawler._capture_payload.await_args_list[0].kwargs == {"city": "桃園市"}
    assert crawler._capture_payload.await_args_list[1].kwargs == {"city": "新竹縣"}
