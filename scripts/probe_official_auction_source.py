#!/usr/bin/env python3
"""Probe an official Taiwan auction source without bypassing access controls.

This standalone script:

1. downloads and evaluates the source's robots.txt;
2. fetches the public auction query page when robots.txt permits it;
3. stops with exit code 2 when an interactive CAPTCHA is present;
4. emits a machine-readable JSON report.

It intentionally does not solve, replay, or submit CAPTCHA values.  A result
parser should only be connected after an approved, unattended official feed is
available.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

DEFAULT_QUERY_URL = "https://www.tpkonsale.moj.gov.tw/Estate/Query"
DEFAULT_ROBOTS_URL = "https://www.tpkonsale.moj.gov.tw/robots.txt"
DEFAULT_USER_AGENT = (
    "PropertyCaseRadar/0.1 "
    "(+https://github.com/xxx789666/property-case-radar)"
)

EXIT_OK = 0
EXIT_CAPTCHA = 2
EXIT_ROBOTS_BLOCKED = 3
EXIT_NETWORK_ERROR = 4


class AccessControlDetector(HTMLParser):
    """Detect common interactive CAPTCHA fields in a public HTML form."""

    def __init__(self) -> None:
        super().__init__()
        self.has_captcha = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            key.lower(): (value or "").lower()
            for key, value in attrs
        }
        searchable = " ".join(
            (
                attributes.get("name", ""),
                attributes.get("id", ""),
                attributes.get("class", ""),
                attributes.get("src", ""),
                attributes.get("alt", ""),
            )
        )
        if "captcha" in searchable or "驗證碼" in searchable:
            self.has_captcha = True

    def handle_data(self, data: str) -> None:
        if "captcha" in data.lower() or "驗證碼" in data:
            self.has_captcha = True


@dataclass(frozen=True)
class ProbeReport:
    source: str
    query_url: str
    robots_url: str
    robots_allowed: bool
    captcha_present: bool
    http_status: int | None
    message: str


def fetch_text(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/plain,text/html;charset=utf-8",
        },
        method="GET",
    )
    tls_context = ssl.create_default_context()
    # Some Taiwan government sites serve a publicly trusted certificate chain
    # whose intermediate CA omits the optional Subject Key Identifier. Python
    # 3.13/OpenSSL may enable X509_STRICT and reject that chain even though the
    # platform trust store accepts it. Keep hostname/signature/CA validation
    # enabled and relax only that additional strictness check.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        tls_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    with urlopen(
        request,
        timeout=timeout_seconds,
        context=tls_context,
    ) as response:
        status = response.status
        content_type = response.headers.get_content_charset() or "utf-8"
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(f"response exceeded {max_bytes} bytes")
        return status, payload.decode(content_type, errors="replace")


def robots_allows(
    robots_text: str,
    *,
    robots_url: str,
    user_agent: str,
    query_url: str,
) -> bool:
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(user_agent, query_url)


def probe(args: argparse.Namespace) -> tuple[int, ProbeReport]:
    robots_status, robots_text = fetch_text(
        args.robots_url,
        user_agent=args.user_agent,
        timeout_seconds=args.timeout,
        max_bytes=args.max_bytes,
    )
    allowed = robots_status == 200 and robots_allows(
        robots_text,
        robots_url=args.robots_url,
        user_agent=args.user_agent,
        query_url=args.query_url,
    )
    if not allowed:
        return EXIT_ROBOTS_BLOCKED, ProbeReport(
            source=args.source,
            query_url=args.query_url,
            robots_url=args.robots_url,
            robots_allowed=False,
            captcha_present=False,
            http_status=None,
            message="robots.txt does not permit unattended access",
        )

    time.sleep(max(1.0, args.delay))
    page_status, html = fetch_text(
        args.query_url,
        user_agent=args.user_agent,
        timeout_seconds=args.timeout,
        max_bytes=args.max_bytes,
    )
    detector = AccessControlDetector()
    detector.feed(html)
    if detector.has_captcha:
        return EXIT_CAPTCHA, ProbeReport(
            source=args.source,
            query_url=args.query_url,
            robots_url=args.robots_url,
            robots_allowed=True,
            captcha_present=True,
            http_status=page_status,
            message="interactive CAPTCHA detected; unattended query disabled",
        )

    return EXIT_OK, ProbeReport(
        source=args.source,
        query_url=args.query_url,
        robots_url=args.robots_url,
        robots_allowed=True,
        captcha_present=False,
        http_status=page_status,
        message=(
            "public query page is reachable; result schema still requires "
            "review before ingestion"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely probe an official Taiwan auction query page.",
    )
    parser.add_argument("--source", default="MOJ Administrative Enforcement")
    parser.add_argument("--query-url", default=DEFAULT_QUERY_URL)
    parser.add_argument("--robots-url", default=DEFAULT_ROBOTS_URL)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between robots.txt and page requests; minimum is 1 second.",
    )
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        exit_code, report = probe(args)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        exit_code = EXIT_NETWORK_ERROR
        report = ProbeReport(
            source=args.source,
            query_url=args.query_url,
            robots_url=args.robots_url,
            robots_allowed=False,
            captcha_present=False,
            http_status=getattr(exc, "code", None),
            message=f"source probe failed: {exc}",
        )

    json.dump(
        asdict(report),
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
