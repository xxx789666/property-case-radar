from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from crawlers.auction.moj_source import (
    MOJ_AUCTION_QUERY_URL,
    MOJ_ROBOTS_URL,
    AuctionSourceBlocked,
    MojAuctionAnnouncementSource,
    ensure_no_interactive_access_control,
)
from crawlers.transaction.moi_open_data import (
    DEFAULT_USER_AGENT,
    MoiActualPriceSource,
    MoiBatch,
    MoiOpenDataError,
    parse_moi_zip,
    sync_market_prices,
)
from database.models.common import MarketPrice

FIXTURES = Path(__file__).parent / "fixtures"


def _zip_with(member: str, text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return output.getvalue()


def _sample_zip() -> bytes:
    return _zip_with(
        "a_lvr_land_a.csv",
        (FIXTURES / "moi_actual_price_sample.csv").read_text(encoding="utf-8"),
    )


def test_moi_parser_uses_current_bounded_batch_and_normalizes_units() -> None:
    records, skipped = parse_moi_zip(_sample_zip(), today=date(2026, 7, 24))
    assert len(records) == 3
    assert skipped == 2
    assert records[0].city == "臺北市"
    assert records[0].district == "中正區"
    assert records[0].unit_price_per_ping_twd == 661_157
    assert records[0].building_area_ping == pytest.approx(30)
    assert records[0].address is None
    assert records[0].building_type == "住宅"
    assert records[1].building_type == "辦公廠房"
    assert records[2].building_type == "土地"
    assert records[2].unit_price_per_ping_twd == 99_174
    assert records[2].building_area_ping == pytest.approx(30.25, rel=0.01)


def test_moi_parser_rejects_unknown_schema_and_invalid_zip() -> None:
    with pytest.raises(MoiOpenDataError, match="schema mismatch"):
        parse_moi_zip(_zip_with("a_lvr_land_a.csv", "unexpected,column\n1,2\n"))
    with pytest.raises(MoiOpenDataError, match="valid ZIP"):
        parse_moi_zip(b"not-a-zip")


@pytest.mark.asyncio
async def test_moi_source_uses_validators_and_cached_304(tmp_path: Path) -> None:
    payload = _sample_zip()
    observed_headers: list[httpx.Headers] = []

    async def first_handler(request: httpx.Request) -> httpx.Response:
        observed_headers.append(request.headers)
        return httpx.Response(
            200,
            content=payload,
            headers={"ETag": '"fixture-etag"', "Last-Modified": "Fri, 24 Jul 2026 00:00:00 GMT"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(first_handler)) as client:
        first = await MoiActualPriceSource(
            cache_dir=tmp_path,
            client=client,
            today=date(2026, 7, 24),
        ).fetch()
    assert first.downloaded is True
    assert observed_headers[0]["user-agent"] == DEFAULT_USER_AGENT

    async def second_handler(request: httpx.Request) -> httpx.Response:
        observed_headers.append(request.headers)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(second_handler)) as client:
        second = await MoiActualPriceSource(
            cache_dir=tmp_path,
            client=client,
            today=date(2026, 7, 24),
        ).fetch()
    assert second.downloaded is False
    assert len(second.records) == 3
    assert observed_headers[1]["if-none-match"] == '"fixture-etag"'
    assert "if-modified-since" in observed_headers[1]


@pytest.mark.asyncio
async def test_market_sync_is_idempotent(session_factory) -> None:
    records, skipped = parse_moi_zip(_sample_zip(), today=date(2026, 7, 24))

    class Source:
        async def fetch(self) -> MoiBatch:
            return MoiBatch(records, True, None, None, skipped)

    with session_factory() as session:
        first = await sync_market_prices(Source(), session)
        second = await sync_market_prices(Source(), session)
        count = session.scalar(select(func.count()).select_from(MarketPrice))
    assert first.created == 3
    assert second.created == 0
    assert second.unchanged == 3
    assert count == 3


def test_moj_captcha_fixture_fails_closed() -> None:
    html = (FIXTURES / "moj_auction_query_captcha.html").read_text(encoding="utf-8")
    with pytest.raises(AuctionSourceBlocked, match="requires CAPTCHA"):
        ensure_no_interactive_access_control(html)


@pytest.mark.asyncio
async def test_moj_adapter_checks_robots_then_stops_at_captcha(monkeypatch) -> None:
    html = (FIXTURES / "moj_auction_query_captcha.html").read_text(encoding="utf-8")
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == MOJ_ROBOTS_URL:
            return httpx.Response(200, text="User-agent: *\nDisallow: /*.jpg$\nDisallow: /*.gif$\n")
        return httpx.Response(200, text=html)

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr("crawlers.auction.moj_source.asyncio.sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MojAuctionAnnouncementSource(client=client)
        with pytest.raises(AuctionSourceBlocked, match="requires CAPTCHA"):
            await source.fetch()
    assert seen == [MOJ_ROBOTS_URL, MOJ_AUCTION_QUERY_URL]


@pytest.mark.asyncio
async def test_moj_adapter_honors_robots_denial_without_fetching_query() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MojAuctionAnnouncementSource(client=client)
        with pytest.raises(AuctionSourceBlocked, match="robots.txt"):
            await source.fetch()
    assert seen == [MOJ_ROBOTS_URL]
