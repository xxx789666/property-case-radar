import pytest

from crawlers.auction.court_crawler import CompliancePolicy, FixtureAuctionAnnouncementSource
from crawlers.auction.document_parser import extract_documents, is_unchanged
from crawlers.auction.parser import CourtAnnouncementParser


@pytest.mark.asyncio
async def test_fetch_returns_all_fixture_files() -> None:
    source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
    announcements = await source.fetch()
    assert len(announcements) == 4
    assert all(a.raw_html.strip() for a in announcements)
    assert all(a.source_url.startswith("file://") for a in announcements)


@pytest.mark.asyncio
async def test_fetch_has_no_network_dependency() -> None:
    import crawlers.auction.court_crawler as module

    assert not hasattr(module, "httpx")
    assert not hasattr(module, "requests")


@pytest.mark.asyncio
async def test_fixture_announcements_are_parseable() -> None:
    source = FixtureAuctionAnnouncementSource("crawlers/auction/fixtures")
    parser = CourtAnnouncementParser()
    for announcement in await source.fetch():
        parsed = parser.parse(announcement.raw_html, source_url=announcement.source_url)
        assert parsed.case_number == "115年度司執字第12345號"


def test_compliance_policy_refuses_bypass() -> None:
    with pytest.raises(ValueError, match="prohibited"):
        CompliancePolicy(bypass_anti_bot=True)
    with pytest.raises(ValueError, match="prohibited"):
        CompliancePolicy(bypass_login=True)


def test_compliance_policy_rejects_invalid_delay_range() -> None:
    with pytest.raises(ValueError, match="invalid crawler delay range"):
        CompliancePolicy(min_delay_seconds=5, max_delay_seconds=1)


def test_document_extraction_and_dedup() -> None:
    parser = CourtAnnouncementParser()
    html = (
        '<article class="court-announcement" data-kind="new"><dl>'
        "<dt>法院</dt><dd>c</dd><dt>案號</dt><dd>n</dd></dl>"
        '<ul class="documents"><li data-type="announcement"><a href="https://x">公告</a></li></ul>'
        "</article>"
    )
    parsed = parser.parse(html)
    docs = extract_documents(parsed)
    assert len(docs) == 1
    assert docs[0].content_hash == parsed.source_hash

    assert is_unchanged({parsed.source_hash}, parsed) is True
    assert is_unchanged({"some-other-hash"}, parsed) is False
    assert is_unchanged(set(), parsed) is False
