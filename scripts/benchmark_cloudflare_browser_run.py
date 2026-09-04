"""Bounded A/B benchmark for Cloudflare Browser Run markdown extraction.

This intentionally uses only public, non-591 pages and never prints credentials.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from urllib.request import Request, urlopen

URLS = [
    "https://example.com",
    "https://www.cloudflare.com/",
    "https://developers.cloudflare.com/browser-run/",
    "https://www.rfc-editor.org/rfc/rfc9110",
    "https://www.python.org/",
] * 4


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def call(account: str, token: str, url: str, browser: str | None) -> dict:
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account}/browser-rendering/markdown"
    if browser:
        endpoint += f"?browser={browser}"
    req = Request(
        endpoint,
        data=json.dumps({"url": url}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(req, timeout=60) as response:
            body = response.read()
            # The markdown endpoint wraps the extracted text in JSON; a non-trivial
            # response is our bounded completeness proxy for this public-page A/B.
            return {
                "status": response.status,
                "bytes": len(body),
                "ok": bool(body),
                "complete": len(body) >= 200,
                "seconds": time.perf_counter() - started,
            }
    except Exception as exc:  # bounded benchmark; preserve error without secrets
        return {"status": None, "bytes": 0, "ok": False, "complete": False, "seconds": time.perf_counter() - started, "error": type(exc).__name__}


def main() -> None:
    env_path = Path(os.getenv("CLOUDFLARE_ENV", r"D:\discord 個人助理\.local\cloudflare.env"))
    env = load_env(env_path)
    account = env.get("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    token = env.get("CLOUDFLARE_API_TOKEN") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    output = Path(os.getenv("CLOUDFLARE_AB_OUTPUT", r"D:\property-case-radar\tmp\cloudflare-browser-run-ab.json"))
    result = {"status": "skipped", "reason": None, "sample_size": len(URLS), "modes": {}}
    if not account or not token:
        result["reason"] = "missing_account_or_token"
    else:
        result["status"] = "ok"
        for mode in ("chromium", "kitesurf"):
            samples = []
            for url in URLS:
                # Chromium is the default; Kitesurf is the explicit opt-in.
                samples.append({"url": url, **call(account, token, url, None if mode == "chromium" else mode)})
            durations = [x["seconds"] for x in samples]
            result["modes"][mode] = {
                "success": sum(bool(x["ok"]) for x in samples),
                "complete": sum(bool(x["complete"]) for x in samples),
                "completeness_rate": sum(bool(x["complete"]) for x in samples) / len(samples),
                "p50_seconds": statistics.median(durations) if durations else None,
                "p95_seconds": sorted(durations)[max(0, int(len(durations) * 0.95) - 1)] if durations else None,
                "estimated_cost_usd": sum(x["seconds"] for x in samples) / 3600 * 0.09,
                "samples": samples,
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output), "sample_size": len(URLS)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
