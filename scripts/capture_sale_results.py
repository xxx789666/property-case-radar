#!/usr/bin/env python3
"""Capture public 591 sale-listing cards with the auction capture workflow.

This deliberately uses only public pages.  It never logs in, solves or
bypasses anti-bot challenges, or calls a private API.  If 591 presents an
access-control page, that city is reported as failed and the run continues.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

DEFAULT_OUTPUT_DIR = Path(r"D:\網頁識別認證\sale_json")
DEFAULT_TIMEOUT_MS = 30_000
REGIONS = {
    "臺北市": 1,
    "基隆市": 2,
    "新北市": 3,
    "新竹市": 4,
    "新竹縣": 5,
    "桃園市": 6,
    "苗栗縣": 7,
    "臺中市": 8,
    "彰化縣": 10,
    "南投縣": 11,
    "雲林縣": 14,
    "嘉義市": 12,
    "嘉義縣": 13,
    "臺南市": 15,
    "屏東縣": 19,
    "高雄市": 17,
    "宜蘭縣": 21,
    "花蓮縣": 23,
    "臺東縣": 22,
    "澎湖縣": 24,
    "金門縣": 25,
    "連江縣": 26,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture public 591 sale listings")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--city", choices=tuple(REGIONS), default="")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        choices=range(1, 9),
        metavar="1-8",
        help="number of counties captured concurrently",
    )
    parser.add_argument("--page-delay", type=float, default=2.5)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--verify-input", type=Path)
    parser.add_argument("--verify-output", type=Path)
    parser.add_argument("--verify-delay", type=float, default=0.5)
    return parser


def normalize_url(raw_url: str) -> str:
    parsed = urlparse(urljoin("https://sale.591.com.tw/", raw_url))
    query = parse_qs(parsed.query)
    allowed_query = {
        key: values
        for key, values in query.items()
        if key in {"regionid", "sectionid", "shType", "type"}
    }
    return urlunparse(
        (
            "https",
            parsed.netloc.lower(),
            parsed.path,
            "",
            urlencode(allowed_query, doseq=True),
            "",
        )
    )


def parse_decimal(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    return parsed if parsed > 0 else None


def property_id_from_url(url: str) -> str | None:
    matches = re.findall(r"(?<!\d)(\d{6,})(?!\d)", urlparse(url).path)
    return matches[-1] if matches else None


def parse_card(city: str, raw: dict[str, str]) -> dict[str, object] | None:
    text = " ".join(str(raw.get("text", "")).split())
    price_text = " ".join(str(raw.get("price_text", "")).split())
    url = normalize_url(str(raw.get("url", "")))
    source_property_id = property_id_from_url(url)
    total_match = re.search(r"([\d,]+(?:\.\d+)?)", str(raw.get("total_price", "")))
    unit_match = re.search(r"([\d,]+(?:\.\d+)?)\s*萬\s*/\s*坪", price_text)
    area_matches = re.findall(r"權狀\s*([\d,]+(?:\.\d+)?)\s*坪", text)
    if not source_property_id or not total_match or not unit_match or not area_matches:
        return None
    total_wan = parse_decimal(total_match.group(1))
    unit_wan = parse_decimal(unit_match.group(1))
    area_ping = parse_decimal(area_matches[0])
    if total_wan is None or unit_wan is None or area_ping is None:
        return None

    district = str(raw.get("district", "")).strip().rstrip("-")
    layout_match = re.search(r"(\d+房\d+廳\d+衛|開放式格局)", text)
    age_match = re.search(r"(?:屋齡\s*)?([\d.]+)\s*年", text)
    floor_match = re.search(r"(\d+樓|\d+F|整棟)(?:\s*/\s*(\d+)(?:樓|F))?", text, re.IGNORECASE)
    type_match = re.search(r"(別墅|透天厝|公寓|華廈|電梯大樓|套房|店面|辦公|廠房)", text)

    return {
        "source": "591",
        "source_property_id": source_property_id,
        "url": url,
        "city": city,
        "district": district,
        "total_price_twd": int(total_wan * 10_000),
        "unit_price_per_ping_twd": int(unit_wan * 10_000),
        "building_area_ping": str(area_ping),
        "address": str(raw.get("address", "")).strip() or district or None,
        "age_years": age_match.group(1) if age_match else None,
        "floor": floor_match.group(1) if floor_match else None,
        "total_floors": (
            int(floor_match.group(2))
            if floor_match and floor_match.group(2)
            else None
        ),
        "layout": layout_match.group(1) if layout_match else None,
        "building_type": type_match.group(1) if type_match else None,
        "usage": "住家",
        "has_parking": "車位" in text,
        "status": "active",
    }


def parse_land_card(city: str, raw: dict[str, str]) -> dict[str, object] | None:
    lines = [
        " ".join(line.split())
        for line in str(raw.get("text", "")).splitlines()
        if line.strip()
    ]
    url = normalize_url(str(raw.get("url", "")))
    id_match = re.search(r"/sale/(\d+)", urlparse(url).path)
    area_line = next(
        (line for line in lines if re.match(r"^[\d,]+(?:\.\d+)?\s*坪", line)),
        "",
    )
    area_match = re.match(
        r"([\d,]+(?:\.\d+)?)\s*坪\s*(.+?)"
        r"(?=都市土地|非都市土地|臨路|$)",
        area_line,
    )
    unit_line = next((line for line in lines if "萬/坪" in line), "")
    unit_match = re.fullmatch(
        r"([\d,]+(?:\.\d+)?)\s*萬\s*/\s*坪",
        unit_line,
    )
    total_line = next(
        (
            line
            for line in lines
            if re.fullmatch(r"[\d,]+(?:\.\d+)?\s*萬", line)
        ),
        "",
    )
    total_match = re.fullmatch(
        r"([\d,]+(?:\.\d+)?)\s*萬",
        total_line,
    )
    location_line = next(
        (
            line
            for line in lines
            if re.match(r"^[\w]{2,4}[區鄉鎮市]-", line)
        ),
        "",
    )
    location_match = re.match(r"([\w]{2,4}[區鄉鎮市])-([^\s]+)", location_line)
    if (
        id_match is None
        or area_match is None
        or unit_match is None
        or total_match is None
        or location_match is None
    ):
        return None
    area_ping = parse_decimal(area_match.group(1))
    total_wan = parse_decimal(total_match.group(1))
    unit_wan = parse_decimal(unit_match.group(1))
    if area_ping is None or total_wan is None or unit_wan is None:
        return None
    land_type = area_match.group(2).strip(" /")
    district = location_match.group(1)
    address = location_match.group(2)
    return {
        "source": "591",
        "source_property_id": id_match.group(1),
        "url": url,
        "city": city,
        "district": district,
        # The existing sale schema requires building_area_ping. For a
        # land-only listing this mirrors land_area_ping so shared total/
        # unit-price filters continue to work without inventing a building.
        "building_area_ping": str(area_ping),
        "land_area_ping": str(area_ping),
        "total_price_twd": int(total_wan * 10_000),
        "unit_price_per_ping_twd": int(unit_wan * 10_000),
        "address": address,
        "building_type": "土地",
        "usage": land_type or "其他土地",
        "has_parking": False,
        "status": "active",
    }


def result_url(region_id: int) -> str:
    return (
        "https://sale.591.com.tw/?"
        + urlencode(
            {
                "regionid": region_id,
                "shType": "list",
                "type": 2,
                "order": "posttime_desc",
            }
        )
    )


def land_result_url(
    region_id: int,
    page_number: int,
    section_id: int | None = None,
) -> str:
    query: dict[str, object] = {
        "kind": 11,
        "region": region_id,
        "type": 2,
        "page": page_number,
        "order": "posttime_desc",
    }
    if section_id is not None:
        query["section"] = section_id
    return (
        "https://land.591.com.tw/list?"
        + urlencode(query)
    )


def discover_land_sections(
    page,
    region_id: int,
    args: argparse.Namespace,
) -> list[tuple[str, int | None]]:
    """Read 591's public district filters and resolve their section IDs.

    The public page does not expose section IDs in the checkbox markup. Clicking
    a district updates the public URL with ``section=<id>``; no private endpoint
    or access-control bypass is used.
    """
    base_url = land_result_url(region_id, 1)
    page.goto(
        base_url,
        wait_until="domcontentloaded",
        timeout=args.timeout_ms,
    )
    page.locator("label.t5-checkbox--housing").first.wait_for(
        state="attached",
        timeout=min(args.timeout_ms, 12_000),
    )
    labels = page.locator("label.t5-checkbox--housing").all_inner_texts()
    district_names = district_names_from_labels(labels)

    sections: list[tuple[str, int | None]] = []
    needs_citywide_fallback = False
    for district in district_names:
        # The 591 SPA replaces the checkbox node after applying a district.
        # Clicking the same label again can therefore target a freshly-rendered
        # unchecked node and leave ``section`` in the URL forever. Reset to the
        # unfiltered public page before resolving each district instead.
        if parse_qs(urlparse(page.url).query).get("section"):
            page.goto(
                base_url,
                wait_until="domcontentloaded",
                timeout=args.timeout_ms,
            )
            page.locator("label.t5-checkbox--housing").first.wait_for(
                state="attached",
                timeout=min(args.timeout_ms, 12_000),
            )
        checkbox = page.get_by_text(district, exact=True).first
        checkbox.click()
        try:
            page.wait_for_function(
                """() => new URL(window.location.href).searchParams.has("section")""",
                timeout=min(args.timeout_ms, 8_000),
            )
        except Exception:
            # Some small/offshore districts are rendered as options even when
            # 591 has no section mapping for them. Keep the city healthy and
            # cover those records through one unfiltered city-level pass.
            needs_citywide_fallback = True
            print(
                f"  land {district}: no section mapping; using citywide fallback",
                file=sys.stderr,
            )
            continue
        section_values = parse_qs(urlparse(page.url).query).get("section", [])
        if section_values and section_values[0].isdigit():
            sections.append((district, int(section_values[0])))
        else:
            needs_citywide_fallback = True
    if needs_citywide_fallback or not sections:
        sections.append(("全區備援", None))
    return sections


def district_names_from_labels(labels: list[str]) -> list[str]:
    district_names: list[str] = []
    for raw_label in labels:
        label = " ".join(raw_label.split())
        if re.search(r"[區鄉鎮市]$", label):
            district_names.append(label)
        elif district_names:
            break
    return district_names


def capture_city(page, city: str, region_id: int, args: argparse.Namespace) -> tuple[list[dict], int]:
    collected: dict[str, dict] = {}
    pages_scraped = 0
    for page_index in range(args.max_pages):
        if page_index == 0:
            page.goto(
                result_url(region_id),
                wait_until="domcontentloaded",
                timeout=args.timeout_ms,
            )
        else:
            first_card = page.locator(
                '.ware-item a[href*="/home/house/detail/"]'
            ).first
            previous_href = first_card.get_attribute("href")
            next_page = page.locator(
                f'a[href*="page={page_index + 1}"]'
            ).first
            if next_page.count() == 0:
                break
            next_page.click()
            page.wait_for_function(
                """previous => {
                    const current = document.querySelector(
                        '.ware-item a[href*="/home/house/detail/"]'
                    );
                    return current && current.href !== previous;
                }""",
                arg=previous_href,
                timeout=args.timeout_ms,
            )
        # Large markets (New Taipei/Taoyuan/Taichung) hydrate their list
        # noticeably later than the initial DOMContentLoaded event.
        try:
            page.locator(".ware-item").first.wait_for(
                state="attached",
                timeout=min(args.timeout_ms, 12_000),
            )
        except Exception:
            # A legitimately empty county has no card; the checks below
            # still distinguish an access-control page from an empty list.
            pass
        page.wait_for_timeout(500)
        body_text = page.locator("body").inner_text(timeout=args.timeout_ms)
        if any(marker in body_text for marker in ("驗證碼", "存取遭拒", "Access Denied", "請完成驗證")):
            raise RuntimeError("591 presented an access-control challenge; refusing to bypass it")
        anchors = page.locator(
            ".ware-item"
        ).evaluate_all(
            """elements => elements.map(element => ({
                url: element.querySelector('a[href*="/home/house/detail/"]')?.href || "",
                text: element.innerText || element.textContent || "",
                total_price: element.querySelector(".ware-item__price-value")?.innerText || "",
                price_text: element.querySelector(".ware-item__price-section")?.innerText || "",
                district: element.querySelector(".ware-item__section")?.innerText || "",
                address: element.querySelector(".ware-item__address")?.innerText || ""
            }))"""
        )
        added = 0
        for raw in anchors:
            parsed = parse_card(city, raw)
            if parsed and parsed["source_property_id"] not in collected:
                collected[str(parsed["source_property_id"])] = parsed
                added += 1
        pages_scraped += 1
        if added == 0:
            break
        if page_index + 1 < args.max_pages:
            time.sleep(args.page_delay)
    return list(collected.values()), pages_scraped


def capture_land_city(
    page,
    city: str,
    region_id: int,
    args: argparse.Namespace,
) -> tuple[list[dict], int]:
    collected: dict[str, dict] = {}
    pages_scraped = 0
    sections = discover_land_sections(page, region_id, args)
    for district_index, (district, section_id) in enumerate(sections):
        district_added = 0
        for page_number in range(1, args.max_pages + 1):
            page.goto(
                land_result_url(region_id, page_number, section_id),
                wait_until="domcontentloaded",
                timeout=args.timeout_ms,
            )
            try:
                page.locator(".item-info").first.wait_for(
                    state="attached",
                    timeout=min(args.timeout_ms, 12_000),
                )
            except Exception:
                pass
            page.wait_for_timeout(500)
            body_text = page.locator("body").inner_text(timeout=args.timeout_ms)
            if any(
                marker in body_text
                for marker in ("驗證碼", "存取遭拒", "Access Denied", "請完成驗證")
            ):
                raise RuntimeError(
                    "591 land presented an access-control challenge; refusing to bypass it"
                )
            cards = page.locator(".item").evaluate_all(
                """elements => elements.map(element => ({
                    url: element.querySelector('a[href*="/sale/"]')?.href || "",
                    text: element.innerText || element.textContent || ""
                }))"""
            )
            added = 0
            for raw in cards:
                parsed = parse_land_card(city, raw)
                if parsed and parsed["source_property_id"] not in collected:
                    collected[str(parsed["source_property_id"])] = parsed
                    added += 1
            pages_scraped += 1
            district_added += added
            if added == 0:
                break
            if page_number < args.max_pages:
                time.sleep(args.page_delay)
        print(
            f"  land {district}: {district_added} listings",
            file=sys.stderr,
        )
        if district_index + 1 < len(sections):
            time.sleep(args.page_delay)
    return list(collected.values()), pages_scraped


def capture_one_city(
    city: str,
    region_id: int,
    args: argparse.Namespace,
    index: int,
    total: int,
) -> tuple[list[dict], dict[str, object]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is required") from exc

    print(f"[{index}/{total}] 擷取 591 {city} 公開出售頁面", file=sys.stderr)
    with sync_playwright() as playwright:
        try:
            listings: list[dict] = []
            sale_pages_scraped = 0
            land_pages_scraped = 0
            source_errors: list[str] = []
            for attempt in range(2):
                browser = playwright.chromium.launch(headless=args.headless)
                try:
                    context = browser.new_context(
                        locale="zh-TW",
                        timezone_id="Asia/Taipei",
                        viewport={"width": 1440, "height": 1200},
                    )
                    page = context.new_page()
                    try:
                        sale_listings: list[dict] = []
                        land_listings: list[dict] = []
                        source_errors = []
                        try:
                            sale_listings, sale_pages_scraped = capture_city(
                                page, city, region_id, args
                            )
                        except Exception as exc:
                            source_errors.append(f"sale:{type(exc).__name__}:{exc}")
                        try:
                            land_listings, land_pages_scraped = capture_land_city(
                                page, city, region_id, args
                            )
                        except Exception as exc:
                            source_errors.append(f"land:{type(exc).__name__}:{exc}")
                        listings = sale_listings + land_listings
                    finally:
                        context.close()
                finally:
                    browser.close()
                if listings:
                    break
                if attempt == 0:
                    time.sleep(args.page_delay)
            status = (
                "error"
                if not listings
                else "partial"
                if source_errors
                else "ok"
            )
            return listings, {
                "city": city,
                "status": status,
                "sale_pages_scraped": sale_pages_scraped,
                "land_pages_scraped": land_pages_scraped,
                "listing_count": len(listings),
                "errors": source_errors,
            }
        except Exception as exc:
            return [], {"city": city, "status": "error", "error": str(exc)}


def capture(args: argparse.Namespace) -> Path:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cities = list(
        ({args.city: REGIONS[args.city]} if args.city else REGIONS).items()
    )
    all_listings: dict[tuple[str, str], dict] = {}
    city_results: list[dict[str, object]] = []
    tasks = [
        (city, region_id, args, index, len(cities))
        for index, (city, region_id) in enumerate(cities, start=1)
    ]

    # A nationwide land run visits every public district filter. Running a
    # small, bounded number of counties in parallel keeps the daily job well
    # inside its one-hour watchdog while avoiding an unbounded browser burst.
    with ThreadPoolExecutor(max_workers=min(args.workers, len(tasks))) as executor:
        for listings, city_result in executor.map(
            lambda values: capture_one_city(*values),
            tasks,
        ):
            city_results.append(city_result)
            for listing in listings:
                all_listings[(listing["source"], listing["source_property_id"])] = listing

    captured_at = datetime.now().astimezone()
    payload = {
        "source": "591-public-sale-pages",
        "captured_at": captured_at.isoformat(),
        "listings": list(all_listings.values()),
        "cities": city_results,
        "successful_cities": sum(item["status"] == "ok" for item in city_results),
        "partial_cities": sum(item["status"] == "partial" for item in city_results),
        "failed_cities": sum(item["status"] == "error" for item in city_results),
        "policy": {
            "public_pages_only": True,
            "login_used": False,
            "access_control_bypassed": False,
        },
    }
    output_path = args.output_dir / f"sale_591_{captured_at:%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def verify_listing_statuses(args: argparse.Namespace) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is required") from exc
    if args.verify_input is None or not args.verify_input.is_file():
        raise ValueError("verify-input must be an existing JSON file")
    raw_items = json.loads(args.verify_input.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError("verify-input must contain a JSON array")

    inactive_markers = (
        "很抱歉，您查詢的物件不存在",
        "可能已關閉或者被刪除",
        "物件已下架",
        "此物件已關閉",
        "此物件不存在",
        "物件找不到了",
    )
    challenge_markers = ("驗證碼", "存取遭拒", "Access Denied", "請完成驗證")
    results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        try:
            context = browser.new_context(
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                viewport={"width": 1440, "height": 1200},
            )
            page = context.new_page()
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                database_id = raw.get("id")
                source_property_id = str(raw.get("source_property_id", ""))
                url = normalize_url(str(raw.get("url", "")))
                result: dict[str, object] = {
                    "id": database_id,
                    "source_property_id": source_property_id,
                    "url": url,
                    "status": "unknown",
                }
                try:
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=args.timeout_ms,
                    )
                    page.wait_for_timeout(750)
                    body_text = " ".join(
                        page.locator("body").inner_text(timeout=args.timeout_ms).split()
                    )
                    http_status = response.status if response is not None else None
                    result["http_status"] = http_status
                    if any(marker in body_text for marker in challenge_markers):
                        result["reason"] = "access_control"
                    elif (
                        http_status in {404, 410}
                        or any(marker in body_text for marker in inactive_markers)
                    ):
                        result["status"] = "inactive"
                        result["reason"] = "listing_unavailable"
                    elif (
                        http_status == 200
                        and source_property_id
                        and f"S{source_property_id}" in body_text
                        and page.locator("h1").count() > 0
                    ):
                        result["status"] = "active"
                        result["reason"] = "listing_detail_present"
                    else:
                        result["reason"] = "unrecognized_page"
                except Exception as exc:
                    result["reason"] = f"verification_error:{type(exc).__name__}"
                results.append(result)
                if args.verify_delay > 0:
                    time.sleep(args.verify_delay)
            context.close()
        finally:
            browser.close()

    verified_at = datetime.now().astimezone()
    output_path = args.verify_output or (
        args.output_dir / f"sale_591_verify_{verified_at:%Y%m%d_%H%M%S}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "source": "591-public-sale-detail-pages",
                "verified_at": verified_at.isoformat(),
                "results": results,
                "policy": {
                    "public_pages_only": True,
                    "login_used": False,
                    "access_control_bypassed": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    args = build_parser().parse_args()
    if args.max_pages < 1 or args.max_pages > 20:
        print("max-pages must be between 1 and 20", file=sys.stderr)
        return 2
    try:
        output = (
            verify_listing_statuses(args)
            if args.verify_input is not None
            else capture(args)
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"抓取失敗：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "output": str(output)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
