#!/usr/bin/env python3
"""Capture public 591 rental cards without login or access-control bypass."""

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
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

DEFAULT_OUTPUT_DIR = Path(r"D:\網頁識別認證\rental_json")
REGIONS = {
    "臺北市": 1, "基隆市": 2, "新北市": 3, "新竹市": 4, "新竹縣": 5,
    "桃園市": 6, "苗栗縣": 7, "臺中市": 8, "彰化縣": 10, "南投縣": 11,
    "嘉義市": 12, "嘉義縣": 13, "雲林縣": 14, "臺南市": 15, "高雄市": 17,
    "屏東縣": 19, "宜蘭縣": 21, "臺東縣": 22, "花蓮縣": 23, "澎湖縣": 24,
    "金門縣": 25, "連江縣": 26,
}
CHALLENGE_MARKERS = ("驗證碼", "存取遭拒", "Access Denied", "請完成驗證")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--city", choices=tuple(REGIONS), default="")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--page-delay", type=float, default=2.5)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--verify-input", type=Path)
    parser.add_argument("--verify-output", type=Path)
    parser.add_argument("--verify-delay", type=float, default=0.5)
    return parser


def result_url(region_id: int, page_number: int) -> str:
    return "https://rent.591.com.tw/list?" + urlencode(
        {"region": region_id, "order": "posttime_desc", "page": page_number}
    )


def normalize_url(raw: str) -> str:
    parsed = urlparse(urljoin("https://rent.591.com.tw/", raw))
    return urlunparse(("https", "rent.591.com.tw", parsed.path, "", "", ""))


def positive_decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def parse_card(city: str, raw: dict[str, object]) -> dict[str, object] | None:
    text = " ".join(str(raw.get("text", "")).split())
    source_id = str(raw.get("source_property_id", "")).strip()
    url = normalize_url(str(raw.get("url", "")))
    if not source_id:
        match = re.search(r"/(\d{6,})$", urlparse(url).path)
        source_id = match.group(1) if match else ""
    rent_match = re.search(r"([\d,]+)\s*元\s*/\s*月", text)
    area_match = re.search(r"([\d,]+(?:\.\d+)?)\s*坪", text)
    location_match = re.fullmatch(
        r"([^-\s]{1,6}[區鄉鎮市])-([^\s]+)",
        str(raw.get("location", "")).strip(),
    )
    if not source_id or rent_match is None or area_match is None or location_match is None:
        return None
    monthly_rent = int(rent_match.group(1).replace(",", ""))
    area = positive_decimal(area_match.group(1))
    if monthly_rent <= 0 or area is None:
        return None
    district, address = location_match.group(1), location_match.group(2)
    layout_match = re.search(r"(\d+房(?:\d+廳)?(?:\d+衛)?)", text)
    floor_match = re.search(r"((?:B?\d+|頂)F)\s*/\s*(\d+)F", text, re.IGNORECASE)
    rental_type = next(
        (
            marker
            for marker in ("整層住家", "獨立套房", "分租套房", "雅房", "車位", "其他")
            if marker in text
        ),
        None,
    )
    landlord_type = next(
        (marker for marker in ("屋主", "仲介", "代理人") if marker in text),
        None,
    )
    feature_markers = (
        "租金補貼", "可開伙", "可養寵物", "有電梯", "有車位",
        "拎包入住", "隨時可遷入", "近捷運", "免服務費",
    )
    features = "、".join(marker for marker in feature_markers if marker in text) or None
    return {
        "source": "591-rent",
        "source_property_id": source_id,
        "url": url,
        "title": str(raw.get("title", "")).strip()[:255] or f"{city}{district}租屋",
        "city": city,
        "district": district,
        "address": address,
        "monthly_rent_twd": monthly_rent,
        "rent_per_ping_twd": max(1, int(Decimal(monthly_rent) / area)),
        "area_ping": str(area),
        "layout": layout_match.group(1) if layout_match else None,
        "floor": floor_match.group(1) if floor_match else None,
        "total_floors": int(floor_match.group(2)) if floor_match else None,
        "rental_type": rental_type,
        "landlord_type": landlord_type,
        "features": features,
        "status": "active",
    }


def capture_city(city: str, region_id: int, args: argparse.Namespace) -> tuple[list[dict], dict]:
    from playwright.sync_api import sync_playwright

    collected: dict[str, dict] = {}
    pages_scraped = 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=args.headless)
            try:
                context = browser.new_context(
                    locale="zh-TW",
                    timezone_id="Asia/Taipei",
                    viewport={"width": 1440, "height": 1200},
                )
                page = context.new_page()
                for page_number in range(1, args.max_pages + 1):
                    page.goto(
                        result_url(region_id, page_number),
                        wait_until="domcontentloaded",
                        timeout=args.timeout_ms,
                    )
                    try:
                        page.locator(".item").first.wait_for(
                            state="attached", timeout=min(args.timeout_ms, 12_000)
                        )
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    body = page.locator("body").inner_text(timeout=args.timeout_ms)
                    if any(marker in body for marker in CHALLENGE_MARKERS):
                        raise RuntimeError("591 presented an access-control challenge")
                    cards = page.locator(".item").evaluate_all(
                        """elements => elements.map(element => ({
                            source_property_id: element.dataset.id || "",
                            url: element.querySelector('a.link')?.href || "",
                            title: element.querySelector('a.link')?.innerText || "",
                            location: [...element.querySelectorAll('.inline-flex-row')]
                                .map(node => (node.innerText || '').trim())
                                .find(value => /^[^-\\s]{1,6}[區鄉鎮市]-\\S+$/.test(value)) || "",
                            text: element.innerText || element.textContent || ""
                        }))"""
                    )
                    added = 0
                    for raw in cards:
                        parsed = parse_card(city, raw)
                        if parsed and parsed["source_property_id"] not in collected:
                            collected[str(parsed["source_property_id"])] = parsed
                            added += 1
                    pages_scraped += 1
                    if added == 0:
                        break
                    if page_number < args.max_pages:
                        time.sleep(args.page_delay)
            finally:
                browser.close()
        return list(collected.values()), {
            "city": city,
            "status": "ok",
            "pages_scraped": pages_scraped,
            "listing_count": len(collected),
        }
    except Exception as exc:
        return [], {"city": city, "status": "error", "error": str(exc)}


def capture(args: argparse.Namespace) -> Path:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cities = list((({args.city: REGIONS[args.city]}) if args.city else REGIONS).items())
    with ThreadPoolExecutor(max_workers=min(args.workers, len(cities))) as executor:
        results = list(
            executor.map(
                lambda values: capture_city(values[0], values[1], args),
                cities,
            )
        )
    all_listings: dict[str, dict] = {}
    city_results: list[dict] = []
    for listings, result in results:
        city_results.append(result)
        for listing in listings:
            all_listings[str(listing["source_property_id"])] = listing
    captured_at = datetime.now().astimezone()
    payload = {
        "source": "591-public-rental-pages",
        "captured_at": captured_at.isoformat(),
        "listings": list(all_listings.values()),
        "cities": city_results,
        "successful_cities": sum(item["status"] == "ok" for item in city_results),
        "failed_cities": sum(item["status"] == "error" for item in city_results),
        "policy": {
            "public_pages_only": True,
            "login_used": False,
            "access_control_bypassed": False,
        },
    }
    output = args.output_dir / f"rental_591_{captured_at:%Y%m%d_%H%M%S}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def verify_statuses(args: argparse.Namespace) -> Path:
    from playwright.sync_api import sync_playwright

    if args.verify_input is None or not args.verify_input.is_file():
        raise ValueError("verify-input must be an existing JSON file")
    items = json.loads(args.verify_input.read_text(encoding="utf-8"))
    results: list[dict[str, object]] = []
    inactive_markers = ("物件已下架", "物件不存在", "查詢的物件不存在", "已成交")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        try:
            page = browser.new_page(locale="zh-TW")
            for item in items:
                url = normalize_url(str(item.get("url", "")))
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                    page.wait_for_timeout(300)
                    body = page.locator("body").inner_text(timeout=args.timeout_ms)
                    if any(marker in body for marker in CHALLENGE_MARKERS):
                        status = "unknown"
                    elif any(marker in body for marker in inactive_markers):
                        status = "inactive"
                    else:
                        status = "active" if re.search(r"[\d,]+\s*元\s*/\s*月", body) else "unknown"
                except Exception:
                    status = "unknown"
                results.append(
                    {
                        "id": item.get("id"),
                        "source_property_id": item.get("source_property_id"),
                        "status": status,
                    }
                )
                time.sleep(args.verify_delay)
        finally:
            browser.close()
    output = args.verify_output or args.output_dir / "rental_status_verify.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
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
    return output


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = verify_statuses(args) if args.verify_input else capture(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", "output": str(output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
