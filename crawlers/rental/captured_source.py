from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sys

from crawlers.capture_retention import remove_expired_capture_json
from crawlers.rental.base import RentalCrawler, RentalListing
from apps.services.performance import measure_stage


logger = logging.getLogger(__name__)


class RentalCaptureError(RuntimeError):
    pass


class CapturedRentalCrawler(RentalCrawler):
    def __init__(
        self,
        script_path: str | Path,
        *,
        output_dir: str | Path,
        max_pages: int = 3,
        other_max_pages: int = 10,
        focus_max_pages: int = 10,
        workers: int = 4,
        python_executable: str = sys.executable,
        timeout_seconds: float = 45 * 60,
        json_retention_days: int = 30,
    ) -> None:
        super().__init__()
        self.script_path = Path(script_path)
        self.output_dir = Path(output_dir)
        self.max_pages = max_pages
        self.other_max_pages = other_max_pages
        self.focus_max_pages = focus_max_pages
        self.focus_districts: tuple[str, ...] = ()
        self.workers = workers
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self.json_retention_days = json_retention_days
        self.last_health_error: str | None = None
        self.last_failed_cities: tuple[str, ...] = ()

    def set_focus_districts(self, values: list[tuple[str, str]]) -> None:
        self.focus_districts = tuple(
            sorted({f"{city}|{district}" for city, district in values})
        )

    async def _capture_payload(self, *, city: str | None = None) -> dict:
        if not self.script_path.is_file():
            raise RentalCaptureError(f"rental capture script not found: {self.script_path}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        focus_args = [
            argument
            for value in self.focus_districts
            for argument in ("--focus-district", value)
        ]
        source_name = f"591-rental:{city}" if city else "591-rental"
        with measure_stage(logger, "retry" if city else "capture", source_name):
            # Keep browser binaries on D: even when launched manually or by
            # a Scheduled Task whose environment does not inherit the shell
            # profile. This prevents Playwright falling back to C:.
            browser_path = Path(__file__).resolve().parents[2] / ".runtime" / "playwright"
            process = await asyncio.create_subprocess_exec(
                self.python_executable,
                str(self.script_path),
                "--output-dir", str(self.output_dir),
                "--max-pages", str(self.max_pages),
                "--other-max-pages", str(self.other_max_pages),
                "--focus-max-pages", str(self.focus_max_pages),
                "--workers", str(self.workers),
                "--headless",
                *(("--city", city) if city else ()),
                *focus_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                        "PLAYWRIGHT_BROWSERS_PATH", str(browser_path)
                    ),
                },
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), self.timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise RentalCaptureError("rental capture timed out") from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace") or stdout_text
            raise RentalCaptureError(detail[-2000:])
        path = result_path(stdout_text)
        with measure_stage(logger, "parse", source_name):
            return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _listings_from_payload(payload: dict) -> list[RentalListing]:
        policy = payload.get("policy", {})
        if policy != {
            "public_pages_only": True,
            "login_used": False,
            "access_control_bypassed": False,
            "rental_kinds": [0, 24],
            "business_rental_kinds": [5, 6, 12, 7],
        }:
            raise RentalCaptureError("rental capture policy is invalid")
        listings: list[RentalListing] = []
        for raw in payload.get("listings", []):
            try:
                values = dict(raw)
                values["area_ping"] = Decimal(str(values["area_ping"]))
                listings.append(RentalListing(**values))
            except (TypeError, ValueError, ArithmeticError):
                continue
        if payload.get("listings") and not listings:
            raise RentalCaptureError("all rental cards failed validation")
        return listings

    async def fetch(self) -> list[RentalListing]:
        payload = await self._capture_payload()
        failed = tuple(
            item["city"]
            for item in payload.get("cities", [])
            if item.get("status") != "ok"
        )
        self.last_failed_cities = failed
        self.last_health_error = (
            "591 租屋部分抓取失敗／待重試：" + "、".join(failed)
            if failed else None
        )
        listings = self._listings_from_payload(payload)
        remove_expired_capture_json(
            self.output_dir, retention_days=self.json_retention_days
        )
        return listings

    async def retry_failed_cities(self, cities: tuple[str, ...]) -> list[RentalListing]:
        """Recapture only failed cities and retain the still-failing subset."""

        listings: list[RentalListing] = []
        unresolved: list[str] = []
        for city in cities:
            try:
                payload = await self._capture_payload(city=city)
                city_failed = any(
                    item.get("status") != "ok"
                    for item in payload.get("cities", [])
                )
                if city_failed:
                    unresolved.append(city)
                    continue
                listings.extend(self._listings_from_payload(payload))
            except Exception:  # noqa: BLE001 - other cities must still retry
                unresolved.append(city)
        self.last_failed_cities = tuple(unresolved)
        self.last_health_error = (
            "591 租屋部分抓取失敗／待重試：" + "、".join(unresolved)
            if unresolved else None
        )
        remove_expired_capture_json(
            self.output_dir, retention_days=self.json_retention_days
        )
        return listings


def result_path(stdout: str) -> Path:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("status") == "ok":
            path = Path(str(payload["output"]))
            if path.is_file():
                return path
    raise RentalCaptureError("capture output path was not reported")
