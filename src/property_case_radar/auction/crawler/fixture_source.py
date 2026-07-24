"""Fixture-backed ``AnnouncementSource`` -- no network calls.

Reads local HTML files from a directory (defaults to
``auction/fixtures``) and returns them as ``RawAnnouncement``. This is
the only ``AnnouncementSource`` implementation shipped in this round,
by design (spec section 十六: avoid unnecessary live crawling; task
instructions for this vertical explicitly call for fixture-backed
testing, not a live source). It is useful both for tests and as a
manual "replay known announcements" tool during development.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from property_case_radar.auction.crawler.base import RawAnnouncement

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


class FixtureCourtAnnouncementSource:
    """Satisfies the ``AnnouncementSource`` protocol by reading local HTML files."""

    def __init__(self, fixture_dir: Path | str = DEFAULT_FIXTURE_DIR, pattern: str = "*.html") -> None:
        self.fixture_dir = Path(fixture_dir)
        self.pattern = pattern

    def fetch_new_announcements(self, since: datetime | None = None) -> Iterable[RawAnnouncement]:
        del since  # fixtures have no real publish timestamps to filter by
        for path in sorted(self.fixture_dir.glob(self.pattern)):
            yield RawAnnouncement(
                source_url=f"file://{path.as_posix()}",
                raw_html=path.read_text(encoding="utf-8"),
                fetched_at=datetime.now(timezone.utc),
            )
