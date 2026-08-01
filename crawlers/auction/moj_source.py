"""Fail-closed adapter probe for the official MOJ auction search.

The Ministry of Justice Administrative Enforcement Agency is the approved
replacement for the robots-prohibited Judicial Yuan site.  Its public search
currently requires a CAPTCHA for every fresh query.  This module makes that
access-control decision explicit and testable: it checks robots.txt, fetches
only the public query form, and refuses to proceed when a CAPTCHA is present.
It never reads, solves, replays, or submits a CAPTCHA.
"""

from __future__ import annotations

import asyncio
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from crawlers.auction.court_crawler import AuctionAnnouncementSource, CompliancePolicy, RawAnnouncement
from crawlers.transaction.moi_open_data import DEFAULT_USER_AGENT

MOJ_AUCTION_QUERY_URL = "https://www.tpkonsale.moj.gov.tw/Estate/Query"
MOJ_ROBOTS_URL = "https://www.tpkonsale.moj.gov.tw/robots.txt"


class AuctionSourceBlocked(RuntimeError):
    """The official source cannot be accessed without violating policy."""


def ensure_no_interactive_access_control(html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    captcha = soup.find(
        lambda tag: tag.name == "input"
        and str(tag.get("name", "")).upper() == "CAPTCHA"
    )
    if captcha is not None:
        raise AuctionSourceBlocked("official MOJ auction search requires CAPTCHA; unattended fetch disabled")


class MojAuctionAnnouncementSource(AuctionAnnouncementSource):
    """Official-source adapter that halts before any controlled query."""

    def __init__(
        self,
        policy: CompliancePolicy | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(policy)
        if self.policy.min_delay_seconds < 1:
            raise ValueError("official-source request interval must be at least one second")
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def fetch(self) -> list[RawAnnouncement]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        )
        try:
            headers = {"User-Agent": self.user_agent, "Accept": "text/plain,text/html"}
            robots = await client.get(MOJ_ROBOTS_URL, headers=headers)
            robots.raise_for_status()
            parser = RobotFileParser()
            parser.set_url(MOJ_ROBOTS_URL)
            parser.parse(robots.text.splitlines())
            if not parser.can_fetch(self.user_agent, MOJ_AUCTION_QUERY_URL):
                raise AuctionSourceBlocked("official MOJ robots.txt disallows the auction query")

            await asyncio.sleep(max(1, self.policy.min_delay_seconds))
            page = await client.get(MOJ_AUCTION_QUERY_URL, headers=headers)
            page.raise_for_status()
            ensure_no_interactive_access_control(page.text)
            # No result-page schema is guessed.  If the source removes its
            # CAPTCHA, a sanitized result fixture and reviewed parser must be
            # added before production ingestion can be enabled.
            raise AuctionSourceBlocked("official MOJ result schema has not been reviewed; unattended fetch disabled")
        finally:
            if owns_client:
                await client.aclose()
