"""Conservative HTTP preflight and 24-hour status cache for public listings."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

CACHE_TTL_SECONDS = 24 * 60 * 60


def http_prefilter(url: str, timeout_seconds: float = 8.0) -> str | None:
    """Return only definitive inactive; all ambiguous responses escalate to browser."""
    try:
        request = Request(url, headers={"User-Agent": "property-case-radar/1.0"})
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status in {404, 410}:
                return "inactive"
    except Exception:
        return None
    return None


def load_status_cache(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_cached(cache: dict[str, dict[str, object]], url: str) -> dict[str, object] | None:
    item = cache.get(url)
    if not isinstance(item, dict) or time.time() - float(item.get("ts", 0)) >= CACHE_TTL_SECONDS:
        return None
    return item


def put_cached(cache: dict[str, dict[str, object]], url: str, result: dict[str, object]) -> None:
    cache[url] = {"ts": time.time(), **result}


def save_status_cache(path: Path, cache: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
