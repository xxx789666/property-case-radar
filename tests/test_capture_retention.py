from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from crawlers.capture_retention import remove_expired_capture_json


def test_removes_only_expired_json_below_capture_directory(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    expired = tmp_path / "old.json"
    nested_expired = tmp_path / "county" / "old.json"
    fresh = tmp_path / "fresh.json"
    unrelated = tmp_path / "old.pdf"
    nested_expired.parent.mkdir()
    for path in (expired, nested_expired, fresh, unrelated):
        path.write_text("fixture", encoding="utf-8")
    old_timestamp = (now - timedelta(days=31)).timestamp()
    fresh_timestamp = (now - timedelta(days=30)).timestamp()
    for path in (expired, nested_expired, unrelated):
        os.utime(path, (old_timestamp, old_timestamp))
    os.utime(fresh, (fresh_timestamp, fresh_timestamp))

    removed = remove_expired_capture_json(
        tmp_path,
        retention_days=30,
        now=now,
    )

    assert removed == 2
    assert not expired.exists()
    assert not nested_expired.exists()
    assert fresh.exists()
    assert unrelated.exists()
