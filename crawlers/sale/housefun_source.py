"""Bounded public-page adapter for HouseFun sale listings."""

from __future__ import annotations

import asyncio
import re
import ssl
import time
from decimal import Decimal
from urllib.robotparser import RobotFileParser

import httpx
import truststore
from bs4 import BeautifulSoup

from crawlers.sale.base import CompliancePolicy, SaleCrawler, SaleListing
from crawlers.sale.captured_status import ListingStatusCheck

HOUSEFUN_BASE_URL = "https://buy.housefun.com.tw"
HOUSEFUN_ROBOTS_URL = f"{HOUSEFUN_BASE_URL}/robots.txt"
HOUSEFUN_USER_AGENT = (
    "PropertyCaseRadar/1.0 "
    "(+https://github.com/xxx789666/property-case-radar)"
)


class HousefunSourceError(RuntimeError):
    pass


def _location(address: str) -> tuple[str, str]:
    normalized = address.strip().replace("台北市", "臺北市").replace("台中市", "臺中市")
    normalized = normalized.replace("台南市", "臺南市").replace("台東縣", "臺東縣")
    match = re.match(r"^(?P<city>.{2,3}[市縣])(?P<district>.+?[區鄉鎮市])", normalized)
    if match is None:
        raise ValueError("address has no city/district")
    return match.group("city"), match.group("district")


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", "").strip())


def _classify(title: str) -> tuple[str | None, str | None]:
    normalized = title.replace("工業地", "工業用地")
    if any(marker in normalized for marker in ("農地", "建地", "工業用地", "土地", "林地")):
        usage = next(
            (
                marker
                for marker in ("農地", "建地", "工業用地", "林地")
                if marker in normalized
            ),
            "其他",
        )
        return "土地", usage
    if "店面" in title or "店舖" in title:
        return "店面", "店面"
    return None, None


def parse_housefun_list_page(html: str) -> list[SaleListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[SaleListing] = []
    for card in soup.select("section.m-list-obj"):
        try:
            link = card.select_one('a[href^="/buy/house/"]')
            address_node = card.select_one("address.address")
            area_node = card.select_one(".ping-number .number")
            price_node = card.select_one(".discount-price .number")
            title_node = card.select_one(".casename a")
            if not all((link, address_node, area_node, price_node, title_node)):
                continue
            identifier_match = re.fullmatch(r"/buy/house/(\d+)", link.get("href", ""))
            if identifier_match is None:
                continue
            address = address_node.get_text(" ", strip=True)
            city, district = _location(address)
            area = _number(area_node.get_text(strip=True))
            total_price = int(_number(price_node.get_text(strip=True)) * 10_000)
            if area <= 0 or total_price <= 0:
                continue
            title = title_node.get_text(" ", strip=True)
            building_type, usage = _classify(title)
            pattern = card.select_one(".ping-pattern .pattern")
            floor = card.select_one(".ping-pattern .floor")
            is_land = building_type == "土地"
            listings.append(
                SaleListing(
                    source="housefun",
                    source_property_id=identifier_match.group(1),
                    url=f"{HOUSEFUN_BASE_URL}{link['href']}",
                    city=city,
                    district=district,
                    address=address,
                    total_price_twd=total_price,
                    unit_price_per_ping_twd=round(total_price / float(area)),
                    building_area_ping=area,
                    land_area_ping=area if is_land else None,
                    floor=floor.get_text(" ", strip=True) if floor else None,
                    layout=pattern.get_text(" ", strip=True) if pattern else None,
                    building_type=building_type,
                    usage=usage,
                    has_parking=card.select_one(".ping-pattern .park") is not None,
                    listed_date=None,
                )
            )
        except (ArithmeticError, TypeError, ValueError):
            continue
    return listings


class HousefunSaleCrawler(SaleCrawler):
    def __init__(
        self,
        *,
        max_pages: int = 3,
        policy: CompliancePolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(policy)
        if not 1 <= max_pages <= 3:
            raise ValueError("HouseFun capture is bounded to 1-3 pages")
        self.max_pages = max_pages
        self.client = client
        self._last_request_at: float | None = None

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        if self._last_request_at is not None:
            remaining = self.policy.min_delay_seconds - (
                time.monotonic() - self._last_request_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()
        response = await client.get(url, headers={"User-Agent": HOUSEFUN_USER_AGENT})
        response.raise_for_status()
        return response

    async def fetch(self) -> list[SaleListing]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )
        try:
            robots_response = await self._get(client, HOUSEFUN_ROBOTS_URL)
            robots = RobotFileParser()
            robots.set_url(HOUSEFUN_ROBOTS_URL)
            robots.parse(robots_response.text.splitlines())
            if not robots.can_fetch(HOUSEFUN_USER_AGENT, f"{HOUSEFUN_BASE_URL}/"):
                raise HousefunSourceError("HouseFun robots.txt disallows public buy pages")
            results: dict[str, SaleListing] = {}
            for page in range(1, self.max_pages + 1):
                url = f"{HOUSEFUN_BASE_URL}/" if page == 1 else f"{HOUSEFUN_BASE_URL}/?pg={page}"
                response = await self._get(client, url)
                for item in parse_housefun_list_page(response.text):
                    results[item.source_property_id] = item
            if not results:
                raise HousefunSourceError("HouseFun public pages contained no recognized listings")
            return list(results.values())
        finally:
            if owns_client:
                await client.aclose()


class HousefunStatusVerifier:
    def __init__(self, *, delay_seconds: float = 2) -> None:
        self.delay_seconds = delay_seconds

    async def verify(self, items: list[ListingStatusCheck]) -> dict[int, str]:
        results: dict[int, str] = {}
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            headers={"User-Agent": HOUSEFUN_USER_AGENT},
        ) as client:
            for index, item in enumerate(items):
                if index:
                    await asyncio.sleep(self.delay_seconds)
                try:
                    response = await client.get(item.url)
                except httpx.HTTPError:
                    results[item.id] = "unknown"
                    continue
                if response.status_code in {404, 410}:
                    results[item.id] = "inactive"
                elif response.status_code == 200 and item.source_property_id in response.url.path:
                    results[item.id] = "active"
                else:
                    results[item.id] = "unknown"
        return results
