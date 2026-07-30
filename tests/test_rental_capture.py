import json
import importlib.util
from pathlib import Path

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
    assert crawler.json_retention_days == 30
