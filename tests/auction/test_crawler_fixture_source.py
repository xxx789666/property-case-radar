from property_case_radar.auction.crawler.base import RateLimiter
from property_case_radar.auction.crawler.fixture_source import DEFAULT_FIXTURE_DIR, FixtureCourtAnnouncementSource
from property_case_radar.auction.parsers.court_announcement import CourtAnnouncementParser


def test_default_fixture_dir_exists() -> None:
    assert DEFAULT_FIXTURE_DIR.is_dir()


def test_fetch_new_announcements_returns_all_fixture_files() -> None:
    source = FixtureCourtAnnouncementSource()
    announcements = list(source.fetch_new_announcements())
    assert len(announcements) == 4
    assert all(a.raw_html.strip() for a in announcements)
    assert all(a.source_url.startswith("file://") for a in announcements)


def test_fetch_new_announcements_no_network_dependency(monkeypatch) -> None:
    # Guard against accidentally introducing a live HTTP call: if either
    # of these were imported/used, this would fail import or the guard
    # below would trip.
    import property_case_radar.auction.crawler.fixture_source as module

    assert not hasattr(module, "httpx")
    assert not hasattr(module, "requests")


def test_fixture_announcements_are_parseable() -> None:
    source = FixtureCourtAnnouncementSource()
    parser = CourtAnnouncementParser()
    for announcement in source.fetch_new_announcements():
        parsed = parser.parse(announcement.raw_html, source_url=announcement.source_url)
        assert parsed.case_number == "115年度司執字第12345號"


def test_rate_limiter_delay_within_bounds() -> None:
    limiter = RateLimiter(min_delay_seconds=1.0, max_delay_seconds=2.0)
    for _ in range(20):
        delay = limiter.next_delay_seconds()
        assert 1.0 <= delay <= 2.0


def test_rate_limiter_rejects_invalid_bounds() -> None:
    import pytest

    with pytest.raises(ValueError):
        RateLimiter(min_delay_seconds=5.0, max_delay_seconds=1.0)


def test_rate_limiter_backoff_grows_with_attempt() -> None:
    limiter = RateLimiter(min_delay_seconds=1.0, max_delay_seconds=1.0)
    assert limiter.retry_backoff_seconds(1) == 1.0
    assert limiter.retry_backoff_seconds(2) == 2.0
    assert limiter.retry_backoff_seconds(3) == 4.0
