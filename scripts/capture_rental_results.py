#!/usr/bin/env python3
"""Capture public 591 rental cards without login or access-control bypass."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from apps.services.status_prefilter import get_cached, http_prefilter, load_status_cache, put_cached, save_status_cache

DEFAULT_OUTPUT_DIR = Path(r"D:\網頁識別認證\rental_json")
REGIONS = {
    "臺北市": 1, "基隆市": 2, "新北市": 3, "新竹市": 4, "新竹縣": 5,
    "桃園市": 6, "苗栗縣": 7, "臺中市": 8, "彰化縣": 10, "南投縣": 11,
    "嘉義市": 12, "嘉義縣": 13, "雲林縣": 14, "臺南市": 15, "高雄市": 17,
    "屏東縣": 19, "宜蘭縣": 21, "臺東縣": 22, "花蓮縣": 23, "澎湖縣": 24,
    "金門縣": 25, "連江縣": 26,
}
CHALLENGE_MARKERS = ("驗證碼", "存取遭拒", "Access Denied", "請完成驗證")
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--city", choices=tuple(REGIONS), default="")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--other-max-pages", type=int, default=10)
    parser.add_argument("--focus-max-pages", type=int, default=10)
    parser.add_argument("--focus-district", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--page-delay", type=float, default=2.5)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--verify-input", type=Path)
    parser.add_argument("--verify-output", type=Path)
    parser.add_argument("--verify-delay", type=float, default=0.5)
    parser.add_argument("--verify-workers", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--status-cache", type=Path)
    return parser


def block_unneeded_resources(route) -> None:
    """Avoid downloading assets that are irrelevant to text/card extraction."""

    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


RENTAL_KINDS = (0, 24)
FOCUS_RENTAL_KINDS = (1, 2, 3, 4, 8, 24)
BUSINESS_RENTAL_KINDS = (5, 6, 12, 7)
BUSINESS_RENTAL_KIND_LABELS = {
    5: "店面",
    6: "辦公",
    12: "住辦",
    7: "廠房",
}
RENTAL_KIND_LABELS = {
    1: "整層住家",
    2: "獨立套房",
    3: "分租套房",
    4: "雅房",
    8: "車位",
    24: "其他",
}


def result_url(
    region_id: int,
    page_number: int,
    rental_kind: int = 0,
    section_id: int | None = None,
) -> str:
    values = {
        "region": region_id,
        "kind": rental_kind,
        "order": "posttime_desc",
        "page": page_number,
    }
    if section_id is not None:
        values["section"] = section_id
    return "https://rent.591.com.tw/list?" + urlencode(
        values
    )


def business_result_url(
    region_id: int,
    page_number: int,
    business_kind: int,
    section_id: int | None = None,
) -> str:
    values = {
        "region": region_id,
        "kind": business_kind,
        "type": 1,
        "order": "posttime_desc",
        "page": page_number,
    }
    if section_id is not None:
        values["section"] = section_id
    return "https://business.591.com.tw/list?" + urlencode(values)


def normalize_url(raw: str) -> str:
    parsed = urlparse(urljoin("https://rent.591.com.tw/", raw))
    return urlunparse(("https", "rent.591.com.tw", parsed.path, "", "", ""))


def normalize_business_url(raw: str) -> str:
    parsed = urlparse(urljoin("https://business.591.com.tw/", raw))
    return urlunparse(("https", "business.591.com.tw", parsed.path, "", "", ""))


def normalize_detail_url(raw: str) -> str:
    if urlparse(raw).hostname == "business.591.com.tw":
        return normalize_business_url(raw)
    return normalize_url(raw)


def positive_decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def parse_area(raw: dict[str, object], text: str) -> Decimal | None:
    area_text = " ".join(str(raw.get("area", "")).split())
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*坪", area_text or text)
    if match is None:
        return None
    area = positive_decimal(match.group(1))
    return area if area is not None and area < Decimal("100000000") else None


def parse_card(city: str, raw: dict[str, object]) -> dict[str, object] | None:
    text = " ".join(str(raw.get("text", "")).split())
    source_id = str(raw.get("source_property_id", "")).strip()
    url = normalize_url(str(raw.get("url", "")))
    if not source_id:
        match = re.search(r"/(\d{6,})$", urlparse(url).path)
        source_id = match.group(1) if match else ""
    rent_match = re.search(r"([\d,]+)\s*元\s*/\s*月", text)
    area = parse_area(raw, text)
    location_match = re.fullmatch(
        r"([^-\s]{1,6}[區鄉鎮市])-([^\s]+)",
        str(raw.get("location", "")).strip(),
    )
    if not source_id or rent_match is None or area is None or location_match is None:
        return None
    monthly_rent = int(rent_match.group(1).replace(",", ""))
    if monthly_rent <= 0:
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


def parse_business_card(
    city: str,
    business_kind: int,
    raw: dict[str, object],
) -> dict[str, object] | None:
    text = " ".join(str(raw.get("text", "")).split())
    url = normalize_detail_url(str(raw.get("url", "")))
    source_id = str(raw.get("source_property_id", "")).strip()
    if not source_id:
        match = re.search(
            r"(?:/rent/|rent-detail-)(\d{6,})(?:\.html)?$",
            urlparse(url).path,
        )
        source_id = match.group(1) if match else ""
    rent_match = re.search(r"([\d,]+)\s*元\s*/\s*月", text)
    area = parse_area(raw, text)
    location_match = re.search(
        r"([^-\s]{1,6}[區鄉鎮市])-([^\s]+)",
        str(raw.get("location", "")).strip(),
    )
    if (
        business_kind not in BUSINESS_RENTAL_KIND_LABELS
        or not source_id
        or rent_match is None
        or area is None
        or location_match is None
    ):
        return None
    monthly_rent = int(rent_match.group(1).replace(",", ""))
    if monthly_rent <= 0:
        return None
    district, address = location_match.group(1), location_match.group(2)
    floor_match = re.search(r"((?:B?\d+|頂|整棟)F?)\s*/\s*(\d+)F", text, re.IGNORECASE)
    landlord_type = next(
        (marker for marker in ("屋主", "仲介", "代理人") if marker in text),
        None,
    )
    feature_markers = (
        "可登記", "可隔間", "可餐飲", "隨時可遷入", "近捷運", "臨街門面",
    )
    business_label = BUSINESS_RENTAL_KIND_LABELS[business_kind]
    features = "、".join(
        [business_label, *(marker for marker in feature_markers if marker in text)]
    )
    return {
        # 591 住辦 detail pages live on rent.591.com.tw and share IDs with
        # the general rental inventory; retain that source to prevent duplicates.
        "source": "591-rent" if business_kind == 12 else "591-business",
        "source_property_id": source_id,
        "url": url,
        "title": str(raw.get("title", "")).strip()[:255] or f"{city}{district}{business_label}",
        "city": city,
        "district": district,
        "address": address,
        "monthly_rent_twd": monthly_rent,
        "rent_per_ping_twd": max(1, int(Decimal(monthly_rent) / area)),
        "area_ping": str(area),
        "layout": None,
        "floor": floor_match.group(1) if floor_match else None,
        "total_floors": int(floor_match.group(2)) if floor_match else None,
        # Existing subscriptions use "其他" for non-residential rentals.
        "rental_type": "其他",
        "landlord_type": landlord_type,
        "features": features,
        "status": "active",
    }


def focus_districts(city: str, values: list[str]) -> list[str]:
    prefix = f"{city}|"
    return sorted(
        {
            value[len(prefix):].strip()
            for value in values
            if value.startswith(prefix) and value[len(prefix):].strip()
        }
    )


def resolve_section_id(
    page,
    region_id: int,
    district: str,
    timeout_ms: int,
) -> int:
    url = result_url(region_id, 1)
    for attempt in range(2):
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        locator = page.locator("button.section.filter-item").filter(
            has_text=district
        )
        locator.first.wait_for(state="visible", timeout=min(timeout_ms, 12_000))
        # The Vue controls can be visible before their click handlers are
        # hydrated. A short readiness pause prevents a visually successful
        # checkbox click that never writes the section query parameter.
        page.wait_for_timeout(1_000 if attempt == 0 else 2_000)
        locator.first.click()
        try:
            page.wait_for_url(
                re.compile(r"[?&]section=\d+"),
                timeout=min(timeout_ms, 5_000),
            )
        except Exception:
            if attempt == 0:
                continue
        values = parse_qs(urlparse(page.url).query).get("section", [])
        if values and values[0].isdigit():
            return int(values[0])
    raise RuntimeError(f"591 section ID not found for {district}")


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
                context.route("**/*", block_unneeded_resources)
                page = context.new_page()
                targets: list[tuple[int, int, int | None]] = [
                    (
                        rental_kind,
                        (
                            args.other_max_pages
                            if rental_kind == 24
                            else args.max_pages
                        ),
                        None,
                    )
                    for rental_kind in RENTAL_KINDS
                ]
                focused_sections: list[int] = []
                for district in focus_districts(city, args.focus_district):
                    section_id = resolve_section_id(
                        page, region_id, district, args.timeout_ms
                    )
                    focused_sections.append(section_id)
                    targets.extend(
                        (
                            rental_kind,
                            args.focus_max_pages,
                            section_id,
                        )
                        for rental_kind in FOCUS_RENTAL_KINDS
                    )
                for rental_kind, page_limit, section_id in targets:
                    for page_number in range(1, page_limit + 1):
                        page.goto(
                            result_url(
                                region_id,
                                page_number,
                                rental_kind,
                                section_id,
                            ),
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
                                area: [...element.querySelectorAll('.item-info-txt, .inline-flex-row')]
                                    .map(node => (node.innerText || '').trim())
                                    .find(value => /^[\\d,.]+\\s*坪/.test(value)) || "",
                                text: element.innerText || element.textContent || ""
                            }))"""
                        )
                        parsed_count = 0
                        for raw in cards:
                            parsed = parse_card(city, raw)
                            if parsed is None:
                                continue
                            parsed_count += 1
                            if rental_kind in RENTAL_KIND_LABELS:
                                parsed["rental_type"] = RENTAL_KIND_LABELS[
                                    rental_kind
                                ]
                            key = f'{parsed["source"]}:{parsed["source_property_id"]}'
                            collected[key] = parsed
                        pages_scraped += 1
                        if parsed_count == 0:
                            break
                        if page_number < page_limit:
                            time.sleep(args.page_delay)
                business_targets = [
                    (business_kind, args.max_pages, None)
                    for business_kind in BUSINESS_RENTAL_KINDS
                ]
                business_targets.extend(
                    (business_kind, args.focus_max_pages, section_id)
                    for section_id in focused_sections
                    for business_kind in BUSINESS_RENTAL_KINDS
                )
                for business_kind, page_limit, section_id in business_targets:
                    for page_number in range(1, page_limit + 1):
                        page.goto(
                            business_result_url(
                                region_id,
                                page_number,
                                business_kind,
                                section_id,
                            ),
                            wait_until="domcontentloaded",
                            timeout=args.timeout_ms,
                        )
                        try:
                            page.locator("main .item").first.wait_for(
                                state="attached", timeout=min(args.timeout_ms, 12_000)
                            )
                        except Exception:
                            pass
                        page.wait_for_timeout(500)
                        body = page.locator("body").inner_text(timeout=args.timeout_ms)
                        if any(marker in body for marker in CHALLENGE_MARKERS):
                            raise RuntimeError("591 Business presented an access-control challenge")
                        cards = page.locator("main .item").evaluate_all(
                            """elements => elements.map(element => {
                                const link = element.querySelector('a.link');
                                const location = [...element.querySelectorAll('.item-info-txt')]
                                    .map(node => (node.innerText || '').trim())
                                    .find(value => /[^-\\s]{1,6}[區鄉鎮市]-\\S+/.test(value)) || "";
                                return {
                                    source_property_id: (link?.href || '').match(/(?:\\/rent\\/|rent-detail-)(\\d+)/)?.[1] || "",
                                    url: link?.href || "",
                                    title: link?.innerText || "",
                                    location,
                                    area: [...element.querySelectorAll('.item-info-txt')]
                                        .map(node => (node.innerText || '').trim())
                                        .find(value => /^[\\d,.]+\\s*坪/.test(value)) || "",
                                    text: element.innerText || element.textContent || ""
                                };
                            })"""
                        )
                        parsed_count = 0
                        for raw in cards:
                            parsed = parse_business_card(city, business_kind, raw)
                            if parsed is None:
                                continue
                            parsed_count += 1
                            key = f'{parsed["source"]}:{parsed["source_property_id"]}'
                            collected[key] = parsed
                        pages_scraped += 1
                        if parsed_count == 0:
                            break
                        if page_number < page_limit:
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
            key = f'{listing["source"]}:{listing["source_property_id"]}'
            all_listings[key] = listing
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
            "rental_kinds": list(RENTAL_KINDS),
            "business_rental_kinds": list(BUSINESS_RENTAL_KINDS),
        },
    }
    output = args.output_dir / f"rental_591_{captured_at:%Y%m%d_%H%M%S}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


async def _verify_status_items(
    args: argparse.Namespace, items: list[dict[str, object]]
) -> list[dict[str, object]]:
    from playwright.async_api import async_playwright

    cache_path = args.status_cache or (args.output_dir / "rental_status_cache.json")
    status_cache = load_status_cache(cache_path)
    results: list[dict[str, object] | None] = [None] * len(items)
    queue: asyncio.Queue[tuple[int, dict[str, object]] | None] = asyncio.Queue()
    for index, item in enumerate(items):
        queue.put_nowait((index, item))

    domain_locks: dict[str, asyncio.Lock] = {}
    next_request_at: dict[str, float] = {}

    async def wait_for_rate_limit(url: str) -> None:
        domain = urlparse(url).netloc.lower()
        lock = domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            delay = max(0.0, next_request_at.get(domain, 0.0) - now)
            if delay:
                await asyncio.sleep(delay)
            next_request_at[domain] = asyncio.get_running_loop().time() + max(
                0.0, args.verify_delay
            )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        context = await browser.new_context(locale="zh-TW")

        async def route_assets(route) -> None:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_assets)

        async def worker() -> None:
            page = await context.new_page()
            try:
                while True:
                    queued = await queue.get()
                    if queued is None:
                        queue.task_done()
                        return
                    index, item = queued
                    url = normalize_detail_url(str(item.get("url", "")))
                    try:
                        cached = get_cached(status_cache, url)
                        if cached:
                            result = {**result, **{k: v for k, v in cached.items() if k != "ts"}}
                            results[index] = result
                            queue.task_done()
                            continue
                        preflight = await asyncio.to_thread(http_prefilter, url)
                        if preflight == "inactive":
                            result.update({"status": "inactive", "reason": "http_prefilter"})
                            put_cached(status_cache, url, result)
                            results[index] = result
                            queue.task_done()
                            continue
                        await wait_for_rate_limit(url)
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=args.timeout_ms,
                        )
                        await page.wait_for_timeout(300)
                        body = await page.locator("body").inner_text(
                            timeout=args.timeout_ms
                        )
                        if any(marker in body for marker in CHALLENGE_MARKERS):
                            status = "unknown"
                        elif any(
                            marker in body
                            for marker in (
                                "物件已下架",
                                "物件不存在",
                                "查詢的物件不存在",
                                "已成交",
                            )
                        ):
                            status = "inactive"
                        else:
                            status = (
                                "active"
                                if re.search(r"[\d,]+\s*元\s*/\s*月", body)
                                else "unknown"
                            )
                    except Exception:
                        status = "unknown"
                    results[index] = {
                        "id": item.get("id"),
                        "source_property_id": item.get("source_property_id"),
                        "status": status,
                    }
                    put_cached(status_cache, url, results[index])
                    queue.task_done()
            finally:
                await page.close()

        worker_count = min(max(1, args.verify_workers), max(1, len(items)))
        tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
        for _ in tasks:
            queue.put_nowait(None)
        await queue.join()
        await asyncio.gather(*tasks)
        await context.close()
        await browser.close()

    save_status_cache(cache_path, status_cache)

    return [item for item in results if item is not None]


def verify_statuses(args: argparse.Namespace) -> Path:
    if args.verify_input is None or not args.verify_input.is_file():
        raise ValueError("verify-input must be an existing JSON file")
    items = json.loads(args.verify_input.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("verify-input must contain a JSON array")
    results = asyncio.run(_verify_status_items(args, items))
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
