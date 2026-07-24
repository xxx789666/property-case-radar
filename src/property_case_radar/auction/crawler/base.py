"""Fetch-layer interface and shared politeness helpers.

Kept separate from parsers/base.py: fetching (network I/O, retries,
rate limiting) and parsing (HTML -> structured data) are independent
concerns and independently testable. A future live source only needs to
produce ``RawAnnouncement`` objects; every parser in ``auction/parsers``
already consumes raw HTML strings, not fetch-specific types.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol


@dataclass(frozen=True)
class RawAnnouncement:
    source_url: str
    raw_html: str
    fetched_at: datetime


class AnnouncementSource(Protocol):
    def fetch_new_announcements(self, since: datetime | None = None) -> Iterable[RawAnnouncement]:
        """Return announcements published/updated since ``since`` (or all, if None).

        Implementations MUST NOT bypass login, paywalls, or anti-bot
        mechanisms (spec section 十六), and MUST only read public
        announcement pages/APIs.
        """
        ...


class RateLimiter:
    """Randomized delay/retry budget for a polite live crawler.

    Not used by the fixture source (no network calls happen there), but
    provided here so a future live ``AnnouncementSource`` implementation
    has a ready-made, tested way to satisfy "避免高頻請求" / "建議加入
    隨機延遲與重試" (spec section 十六) instead of inventing its own.
    """

    def __init__(self, min_delay_seconds: float = 2.0, max_delay_seconds: float = 5.0, max_retries: int = 3) -> None:
        if min_delay_seconds < 0 or max_delay_seconds < min_delay_seconds:
            raise ValueError("require 0 <= min_delay_seconds <= max_delay_seconds")
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.max_retries = max_retries

    def next_delay_seconds(self) -> float:
        return random.uniform(self.min_delay_seconds, self.max_delay_seconds)

    def retry_backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff for ``attempt`` in [1, max_retries]."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        return self.next_delay_seconds() * (2 ** (attempt - 1))
