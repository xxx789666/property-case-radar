from pathlib import Path
import json

from apps.services.status_prefilter import get_cached, load_status_cache, put_cached, save_status_cache


def test_status_cache_roundtrip_and_ttl(tmp_path: Path):
    path = tmp_path / "status.json"
    cache = {}
    put_cached(cache, "https://example.com/a", {"status": "active"})
    save_status_cache(path, cache)
    loaded = load_status_cache(path)
    assert get_cached(loaded, "https://example.com/a")["status"] == "active"


def test_corrupt_cache_is_ignored(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_status_cache(path) == {}
