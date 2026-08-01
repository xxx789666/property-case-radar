"""Restricted client for uploading captured court-original auction PDFs.

This process never receives a Discord token and cannot choose a local path.
It sends validated case coordinates to the loopback-only upload broker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

BROKER_URL_ENV_VAR = "RADAR_PDF_UPLOAD_BROKER_URL"


def _broker_endpoint() -> str:
    base = os.environ.get(BROKER_URL_ENV_VAR, "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("PDF upload broker must be a loopback HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("PDF upload broker URL contains unsupported components")
    return f"{base}/upload-auction-pdf"


def _upload(args: argparse.Namespace) -> dict:
    payload = json.dumps(
        {
            "thread_id": args.thread_id,
            "city": args.city,
            "district": args.district,
            "case_number": args.case_number,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        _broker_endpoint(),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read(64 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024).decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"PDF upload broker returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"PDF upload broker unavailable: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("PDF upload broker returned an invalid response")
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-agent-pdf")
    subparsers = parser.add_subparsers(dest="command", required=True)
    upload = subparsers.add_parser("upload")
    upload.add_argument("--thread-id", required=True)
    upload.add_argument("--city", required=True)
    upload.add_argument("--district", required=True)
    upload.add_argument("--case-number", required=True)
    upload.set_defaults(handler=_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (ValueError, RuntimeError) as exc:
        print(f"radar_agent_pdf: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
