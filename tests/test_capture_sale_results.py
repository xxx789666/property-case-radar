from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "capture_sale_results.py"
SPEC = importlib.util.spec_from_file_location("capture_sale_results", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def test_land_result_url_supports_district_section() -> None:
    url = capture.land_result_url(6, 3, 70)

    assert "region=6" in url
    assert "section=70" in url
    assert "page=3" in url


def test_district_labels_stop_before_non_location_filters() -> None:
    labels = ["桃園區", " 中壢區 ", "楊梅區", "1000萬以下", "住宅區"]

    assert capture.district_names_from_labels(labels) == [
        "桃園區",
        "中壢區",
        "楊梅區",
    ]


def test_capture_defaults_to_bounded_parallel_counties() -> None:
    args = capture.build_parser().parse_args([])

    assert args.workers == 4
