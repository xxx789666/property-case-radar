"""Court announcement fetch layer for the auction pipeline.

COMPLIANCE (taiwan_real_estate_radar.md section 十六 "法拍公告"): mirrors
``crawlers.sale.base.CompliancePolicy``/``SaleCrawler``'s contract --
implementations must honor the policy and must not bypass authentication,
paywalls, CAPTCHAs, rate limits, or other access controls.

Only ``FixtureAuctionAnnouncementSource`` is implemented in this round: it
reads local HTML files from ``crawlers/auction/fixtures`` and performs no
network calls whatsoever. A live implementation (httpx/Playwright per spec
section 十三) can be added later as another ``AuctionAnnouncementSource``
subclass; it must keep the compliance constraints above and should be
reviewed before being pointed at a real court site.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class CompliancePolicy:
    public_pages_only: bool = True
    bypass_login: bool = False
    bypass_anti_bot: bool = False
    min_delay_seconds: float = 2
    max_delay_seconds: float = 5
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.bypass_login or self.bypass_anti_bot:
            raise ValueError("login and anti-bot bypass are prohibited")
        if self.min_delay_seconds < 0 or self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("invalid crawler delay range")

    def next_delay_seconds(self) -> float:
        return random.uniform(self.min_delay_seconds, self.max_delay_seconds)


@dataclass(frozen=True)
class RawAnnouncement:
    source_url: str
    raw_html: str
    fetched_at: datetime


class AuctionAnnouncementSource(ABC):
    """Contract for approved/public court announcement sources.

    Implementations must honor the policy and must not bypass
    authentication, paywalls, CAPTCHAs, rate limits, or other access
    controls -- mirrors ``crawlers.sale.base.SaleCrawler``.
    """

    def __init__(self, policy: CompliancePolicy | None = None):
        self.policy = policy or CompliancePolicy()

    @abstractmethod
    async def fetch(self) -> list[RawAnnouncement]:
        raise NotImplementedError


class FixtureAuctionAnnouncementSource(AuctionAnnouncementSource):
    """Offline adapter used for deterministic development and tests.

    Reads local HTML files from ``fixture_dir`` -- no network calls happen
    here, by design (spec section 十六: avoid unnecessary live crawling).
    """

    def __init__(
        self,
        fixture_dir: str | Path,
        policy: CompliancePolicy | None = None,
        pattern: str = "*.html",
    ) -> None:
        super().__init__(policy)
        self.fixture_dir = Path(fixture_dir)
        self.pattern = pattern

    async def fetch(self) -> list[RawAnnouncement]:
        results: list[RawAnnouncement] = []
        for path in sorted(self.fixture_dir.glob(self.pattern)):
            results.append(
                RawAnnouncement(
                    source_url=f"file://{path.as_posix()}",
                    raw_html=path.read_text(encoding="utf-8"),
                    fetched_at=datetime.now(timezone.utc),
                )
            )
        return results
