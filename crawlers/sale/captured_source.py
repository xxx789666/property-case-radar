"""Adapter for the operator-provided public 591 Playwright capture script."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from crawlers.capture_retention import remove_expired_capture_json
from crawlers.sale.base import CompliancePolicy, SaleCrawler, SaleListing
from apps.services.performance import measure_stage

logger = logging.getLogger(__name__)


class SaleCaptureError(RuntimeError):
    pass


class CapturedSaleCrawler(SaleCrawler):
    def __init__(
        self,
        script_path: str | Path,
        *,
        output_dir: str | Path,
        max_pages: int = 3,
        workers: int = 4,
        city: str | None = None,
        python_executable: str = sys.executable,
        timeout_seconds: float = 60 * 60,
        json_retention_days: int = 30,
        policy: CompliancePolicy | None = None,
    ) -> None:
        super().__init__(policy)
        self.script_path = Path(script_path)
        self.output_dir = Path(output_dir)
        self.max_pages = max_pages
        self.workers = workers
        self.city = city
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self.json_retention_days = json_retention_days
        self.last_health_error: str | None = None
        self.last_failed_cities: tuple[str, ...] = ()

    async def _capture_result_path(self, *, city: str | None = None) -> Path:
        if not self.script_path.is_file():
            raise SaleCaptureError(f"sale capture script not found: {self.script_path}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        arguments = [
            self.python_executable,
            str(self.script_path),
            "--output-dir",
            str(self.output_dir),
            "--max-pages",
            str(self.max_pages),
            "--workers",
            str(self.workers),
            "--headless",
        ]
        selected_city = city or self.city
        if selected_city:
            arguments.extend(["--city", selected_city])
        browser_path = Path(__file__).resolve().parents[2] / ".runtime" / "playwright"
        process = await asyncio.create_subprocess_exec(
            *arguments,
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
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SaleCaptureError(
                f"sale capture exceeded {self.timeout_seconds:g} seconds"
            ) from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() or stdout_text.strip()
            raise SaleCaptureError(
                f"sale capture exited with status {process.returncode}: {detail[-2000:]}"
            )
        return _result_path_from_stdout(stdout_text)

    async def fetch(self) -> list[SaleListing]:
        self.last_health_error = None
        self.last_failed_cities = ()
        with measure_stage(logger, "capture", "591-sale"):
            result_path = await self._capture_result_path()
        with measure_stage(logger, "parse", "591-sale") as measurement:
            listings = self.load_result(result_path)
            measurement.items = len(listings)
        try:
            removed = remove_expired_capture_json(
                self.output_dir,
                retention_days=self.json_retention_days,
            )
            logger.info("sale capture JSON retention removed %s expired files", removed)
        except OSError:
            logger.exception("sale capture JSON retention cleanup failed")
        return listings

    async def retry_failed_cities(self, cities: tuple[str, ...]) -> list[SaleListing]:
        """Recapture incomplete cities and retain only the unresolved subset."""

        listings: list[SaleListing] = []
        unresolved: list[str] = []
        for city in cities:
            try:
                with measure_stage(logger, "retry", f"591-sale:{city}"):
                    result_path = await self._capture_result_path(city=city)
                with measure_stage(logger, "parse", f"591-sale:{city}") as measurement:
                    city_listings = self.load_result(result_path)
                    measurement.items = len(city_listings)
                listings.extend(city_listings)
                if self.last_failed_cities:
                    unresolved.append(city)
            except Exception:  # noqa: BLE001 - retry every remaining city
                logger.exception("sale city retry failed: %s", city)
                unresolved.append(city)

        self.last_failed_cities = tuple(unresolved)
        self.last_health_error = (
            "591 部分抓取失敗／待重試：" + "、".join(unresolved)
            if unresolved
            else None
        )
        remove_expired_capture_json(
            self.output_dir, retention_days=self.json_retention_days
        )
        return listings

    def load_result(self, result_path: str | Path) -> list[SaleListing]:
        self.last_health_error = None
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        policy = payload.get("policy", {})
        if (
            not isinstance(policy, dict)
            or policy.get("public_pages_only") is not True
            or policy.get("login_used") is not False
            or policy.get("access_control_bypassed") is not False
        ):
            raise SaleCaptureError("capture output does not satisfy the public-page policy")
        raw_listings = payload.get("listings")
        if not isinstance(raw_listings, list):
            raise SaleCaptureError("capture output has no listings array")
        if raw_listings and not any(
            isinstance(city, dict) and city.get("status") in {"ok", "partial"}
            for city in payload.get("cities", [])
        ):
            raise SaleCaptureError("capture output has no successful city")
        incomplete = [
            str(city.get("city", "unknown"))
            for city in payload.get("cities", [])
            if isinstance(city, dict) and city.get("status") != "ok"
        ]
        self.last_failed_cities = tuple(incomplete)
        if incomplete:
            self.last_health_error = (
                "591 部分抓取失敗／待重試：" + "、".join(incomplete)
            )

        listings: list[SaleListing] = []
        for raw in raw_listings:
            if not isinstance(raw, dict):
                continue
            try:
                values = dict(raw)
                for field in ("building_area_ping", "land_area_ping", "age_years"):
                    if values.get(field) is not None:
                        values[field] = Decimal(str(values[field]))
                if values.get("listed_date"):
                    values["listed_date"] = date.fromisoformat(str(values["listed_date"]))
                listings.append(SaleListing(**values))
            except (TypeError, ValueError, ArithmeticError):
                continue
        if raw_listings and not listings:
            raise SaleCaptureError("all captured listings failed validation")
        return listings


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
            raise SaleCaptureError(f"capture result file not found: {path}")
    raise SaleCaptureError("capture program did not report a JSON output path")
