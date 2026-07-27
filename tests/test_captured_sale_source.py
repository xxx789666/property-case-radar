from __future__ import annotations

import json
from decimal import Decimal

import pytest

from crawlers.sale.captured_source import CapturedSaleCrawler, SaleCaptureError


def _payload() -> dict:
    return {
        "source": "591-public-sale-pages",
        "policy": {
            "public_pages_only": True,
            "login_used": False,
            "access_control_bypassed": False,
        },
        "cities": [{"city": "新竹縣", "status": "ok"}],
        "listings": [
            {
                "source": "591",
                "source_property_id": "20627018",
                "url": "https://sale.591.com.tw/home/house/detail/2/20627018.html",
                "city": "新竹縣",
                "district": "湖口鄉",
                "total_price_twd": 8_680_000,
                "unit_price_per_ping_twd": 302_300,
                "building_area_ping": "34.13",
                "address": "嘉興路一段95巷",
                "status": "active",
            }
        ],
    }


def test_loads_operator_capture_into_sale_listing(tmp_path) -> None:
    path = tmp_path / "sale.json"
    path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    crawler = CapturedSaleCrawler("capture.py", output_dir=tmp_path)
    listings = crawler.load_result(path)
    assert len(listings) == 1
    assert listings[0].district == "湖口鄉"
    assert listings[0].building_area_ping == Decimal("34.13")


def test_rejects_capture_that_claims_access_control_bypass(tmp_path) -> None:
    payload = _payload()
    payload["policy"]["access_control_bypassed"] = True
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    crawler = CapturedSaleCrawler("capture.py", output_dir=tmp_path)
    with pytest.raises(SaleCaptureError, match="public-page policy"):
        crawler.load_result(path)
