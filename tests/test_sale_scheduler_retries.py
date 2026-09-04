from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from apps.config import Settings
from apps.scheduler.main import make_live_sale_job
from crawlers.sale.base import SaleCrawler, SaleListing
from crawlers.sale.composite import CompositeSaleCrawler


def _listing() -> SaleListing:
    return SaleListing(
        source="591",
        source_property_id="retry-1",
        url="https://sale.591.com.tw/home/house/detail/2/retry-1.html",
        city="臺北市",
        district="大安區",
        total_price_twd=10_000_000,
        unit_price_per_ping_twd=400_000,
        building_area_ping=Decimal("25"),
    )


class PartialSaleSource(SaleCrawler):
    def __init__(self, *, recover: bool) -> None:
        super().__init__()
        self.recover = recover
        self.retry_calls = 0
        self.last_failed_cities: tuple[str, ...] = ()
        self.last_health_error: str | None = None

    async def fetch(self) -> list[SaleListing]:
        self.last_failed_cities = ("臺北市",)
        self.last_health_error = "591 部分抓取失敗／待重試：臺北市"
        return [_listing()]

    async def retry_failed_cities(
        self, cities: tuple[str, ...]
    ) -> list[SaleListing]:
        assert cities == ("臺北市",)
        self.retry_calls += 1
        if self.recover:
            self.last_failed_cities = ()
            self.last_health_error = None
        return [_listing()]


class RecordingDeferredRetries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def schedule(self, **values: object) -> bool:
        self.calls.append(values)
        return True

def test_partial_sale_source_retries_and_recovers(session_factory, tmp_path) -> None:
    source = PartialSaleSource(recover=True)
    crawler = CompositeSaleCrawler({"591": source})
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
        sale_failed_retry_delay_minutes=15,
        sale_failed_retry_rounds=3,
    )

    with patch("apps.scheduler.main.asyncio.sleep", AsyncMock()) as sleep_mock:
        make_live_sale_job(session_factory, crawler, settings)()

    assert source.retry_calls == 1
    sleep_mock.assert_awaited_once_with(900)
    assert crawler.last_failures == {}


def test_unresolved_partial_sale_source_fails_job_for_outer_recovery(
    session_factory, tmp_path
) -> None:
    source = PartialSaleSource(recover=False)
    crawler = CompositeSaleCrawler({"591": source})
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
        sale_failed_retry_delay_minutes=1,
        sale_failed_retry_rounds=2,
    )

    with (
        patch("apps.scheduler.main.asyncio.sleep", AsyncMock()) as sleep_mock,
        pytest.raises(RuntimeError, match="sale sources remain incomplete"),
    ):
        make_live_sale_job(session_factory, crawler, settings)()

    assert source.retry_calls == 2
    assert sleep_mock.await_count == 2


def test_partial_sale_source_schedules_nonblocking_date_retry(
    session_factory, tmp_path
) -> None:
    source = PartialSaleSource(recover=True)
    crawler = CompositeSaleCrawler({"591": source})
    deferred = RecordingDeferredRetries()
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
        sale_failed_retry_delay_minutes=15,
        sale_failed_retry_rounds=3,
    )

    with patch("apps.scheduler.main.asyncio.sleep", AsyncMock()) as sleep_mock:
        make_live_sale_job(
            session_factory,
            crawler,
            settings,
            deferred_retries=deferred,
        )()

    assert source.retry_calls == 0
    sleep_mock.assert_not_awaited()
    assert len(deferred.calls) == 1
    assert deferred.calls[0]["key"] == "sale-591"
    assert deferred.calls[0]["regions"] == ("臺北市",)
