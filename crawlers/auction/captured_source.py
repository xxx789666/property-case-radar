"""Adapter for the operator-provided MOJ Playwright capture script."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from pypdf import PdfReader
from scripts.umi_ocr_service import launch_umi_ocr, umi_ocr_ready

from crawlers.capture_retention import remove_expired_capture_json
from crawlers.auction.court_crawler import (
    AuctionAnnouncementSource,
    CompliancePolicy,
    RawAnnouncement,
)

logger = logging.getLogger(__name__)


class AuctionCaptureError(RuntimeError):
    """The external capture program failed or returned unusable output."""


class CapturedAuctionAnnouncementSource(AuctionAnnouncementSource):
    """Run ``capture_auction_results.py`` and expose its saved detail HTML.

    The capture program owns browser automation, CAPTCHA handling, paging and
    downloads.  This adapter deliberately treats it as a subprocess so its
    Playwright sync API cannot block or conflict with the scheduler event loop.
    """

    def __init__(
        self,
        script_path: str | Path,
        *,
        output_dir: str | Path,
        download_dir: str | Path,
        umi_ocr_url: str = "http://127.0.0.1:1224/api/ocr",
        umi_ocr_executable: str | Path = r"D:\Umi-OCR_Paddle_v2.1.5\Umi-OCR.exe",
        umi_ocr_startup_timeout_seconds: float = 45,
        python_executable: str = sys.executable,
        timeout_seconds: float = 4 * 60 * 60,
        json_retention_days: int = 30,
        policy: CompliancePolicy | None = None,
    ) -> None:
        super().__init__(policy)
        self.script_path = Path(script_path)
        self.output_dir = Path(output_dir)
        self.download_dir = Path(download_dir)
        self.umi_ocr_url = umi_ocr_url
        self.umi_ocr_executable = Path(umi_ocr_executable)
        self.umi_ocr_startup_timeout_seconds = umi_ocr_startup_timeout_seconds
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self.json_retention_days = json_retention_days
        self.last_failed_counties: tuple[str, ...] = ()

    async def fetch(self) -> list[RawAnnouncement]:
        if not self.script_path.is_file():
            raise AuctionCaptureError(f"auction capture script not found: {self.script_path}")

        await self._ensure_ocr_ready()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        result_path = await self._run_capture_process()
        announcements = self.load_result(result_path)
        try:
            removed = remove_expired_capture_json(
                self.output_dir,
                retention_days=self.json_retention_days,
            )
            logger.info("auction capture JSON retention removed %s expired files", removed)
        except OSError:
            logger.exception("auction capture JSON retention cleanup failed")
        return announcements

    async def retry_failed_counties(
        self,
        counties: tuple[str, ...],
    ) -> list[RawAnnouncement]:
        """Retry only failed counties once and retain the still-failed set."""

        await self._ensure_ocr_ready()
        announcements: list[RawAnnouncement] = []
        remaining: list[str] = []
        for county in counties:
            county_output = self.output_dir / county
            county_downloads = self.download_dir / county
            county_output.mkdir(parents=True, exist_ok=True)
            county_downloads.mkdir(parents=True, exist_ok=True)
            try:
                result_path = await self._run_capture_process(
                    "--output-dir",
                    str(county_output),
                    "--download-dir",
                    str(county_downloads),
                    "--single-query",
                    "--county",
                    county,
                    "--auto-query",
                    "--auto-paginate",
                    "--download-files",
                )
                announcements.extend(self.load_result(result_path))
            except AuctionCaptureError:
                remaining.append(county)
        self.last_failed_counties = tuple(remaining)
        return announcements

    async def _run_capture_process(self, *extra_args: str) -> Path:
        process = await asyncio.create_subprocess_exec(
            self.python_executable,
            str(self.script_path),
            "--output-dir",
            str(self.output_dir),
            "--download-dir",
            str(self.download_dir),
            "--umi-ocr-url",
            self.umi_ocr_url,
            "--headless",
            *extra_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AuctionCaptureError(
                f"auction capture exceeded {self.timeout_seconds:g} seconds"
            ) from exc

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr_text.strip() or stdout_text.strip() or "no diagnostic output"
            raise AuctionCaptureError(
                f"auction capture exited with status {process.returncode}: {detail[-2000:]}"
            )

        return _result_path_from_stdout(stdout_text)

    def load_result(self, result_path: str | Path) -> list[RawAnnouncement]:
        """Load a completed capture summary without starting another browser run."""

        result_path = Path(result_path)
        self.last_failed_counties = _failed_counties_from_capture_summary(result_path)
        payloads = _load_capture_payloads(result_path)
        announcements: list[RawAnnouncement] = []
        seen: set[Path] = set()
        for payload in payloads:
            fetched_at = _parse_timestamp(payload.get("captured_at"))
            query_params = payload.get("query_params")
            captured_city = (
                str(query_params.get("county", ""))
                if isinstance(query_params, dict)
                else ""
            )
            for download in payload.get("downloads", []):
                if not isinstance(download, dict) or not download.get("detail_html"):
                    continue
                html_path = Path(str(download["detail_html"]))
                resolved = html_path.resolve()
                if resolved in seen or not html_path.is_file():
                    continue
                seen.add(resolved)
                source_url = str(download.get("detail_url") or resolved.as_uri())
                metadata = {
                    key: value
                    for key, value in {
                        "radar_city": captured_city,
                        "radar_district": str(download.get("district", "")),
                    }.items()
                    if value and value != "未分類"
                }
                if metadata:
                    split = urlsplit(source_url)
                    source_url = urlunsplit(
                        (split.scheme, split.netloc, split.path, split.query, urlencode(metadata))
                    )
                raw_html = html_path.read_text(encoding="utf-8")
                pdf_texts: list[str] = []
                for pdf in download.get("announcement_pdfs", []):
                    if not isinstance(pdf, dict) or not pdf.get("local_path"):
                        continue
                    pdf_path = Path(str(pdf["local_path"]))
                    if not pdf_path.is_file():
                        continue
                    try:
                        text = "\n".join(
                            page.extract_text() or "" for page in PdfReader(pdf_path).pages
                        )
                    except Exception:
                        continue
                    if text.strip():
                        pdf_texts.append(text)
                if pdf_texts:
                    encoded = html.escape(
                        json.dumps(pdf_texts, ensure_ascii=False), quote=False
                    )
                    raw_html += (
                        '\n<script id="radar-official-pdf-text" '
                        f'type="application/json">{encoded}</script>'
                    )
                announcements.append(
                    RawAnnouncement(
                        source_url=source_url,
                        raw_html=raw_html,
                        fetched_at=fetched_at,
                    )
                )
        return announcements

    async def _ensure_ocr_ready(self) -> None:
        if await self._ocr_is_ready():
            return
        if not self.umi_ocr_executable.is_file():
            raise AuctionCaptureError(
                f"Umi-OCR is unavailable and executable was not found: {self.umi_ocr_executable}"
            )

        await asyncio.to_thread(self._launch_ocr)
        deadline = time.monotonic() + self.umi_ocr_startup_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if await self._ocr_is_ready():
                return
        raise AuctionCaptureError(
            "Umi-OCR did not become ready at "
            f"{self.umi_ocr_url} within {self.umi_ocr_startup_timeout_seconds:g} seconds"
        )

    async def _ocr_is_ready(self) -> bool:
        return await asyncio.to_thread(
            umi_ocr_ready,
            self.umi_ocr_url,
            10,
            executable=self.umi_ocr_executable,
        )

    def _launch_ocr(self) -> None:
        try:
            launch_umi_ocr(self.umi_ocr_executable)
        except OSError as exc:
            raise AuctionCaptureError(
                f"failed to start Umi-OCR at {self.umi_ocr_executable}: {exc}"
            ) from exc


def _result_path_from_stdout(stdout: str) -> Path:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("output"):
            path = Path(str(payload["output"]))
            if path.is_file():
                return path
            raise AuctionCaptureError(f"capture result file not found: {path}")
    raise AuctionCaptureError("capture program did not report a JSON output path")


def _load_capture_payloads(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AuctionCaptureError(f"invalid capture JSON root in {path}")
    if isinstance(payload.get("downloads"), list):
        return [payload]

    results: list[dict[str, Any]] = []
    for county in payload.get("counties", []):
        if not isinstance(county, dict) or county.get("status") != "ok" or not county.get("json"):
            continue
        county_path = Path(str(county["json"]))
        county_payload = json.loads(county_path.read_text(encoding="utf-8"))
        if isinstance(county_payload, dict):
            results.append(county_payload)
    if not results and payload.get("failed_counties"):
        raise AuctionCaptureError(f"all county captures failed; see {path}")
    return results


def _failed_counties_from_capture_summary(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("counties"), list):
        return ()

    failed: list[str] = []
    for county in payload["counties"]:
        if not isinstance(county, dict) or county.get("status") == "ok":
            continue
        name = str(county.get("county") or "").strip().replace("台", "臺")
        if name and name not in failed:
            failed.append(name)
    return tuple(failed)


def _parse_timestamp(value: object) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
