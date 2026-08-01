"""Retention cleanup for timestamped crawler JSON artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def remove_expired_capture_json(
    directory: str | Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete only JSON files older than the configured retention period."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    root = Path(directory)
    if not root.is_dir():
        return 0

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for path in root.rglob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified >= cutoff:
            continue
        path.unlink()
        removed += 1
    return removed
