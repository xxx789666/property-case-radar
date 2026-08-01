"""Subprocess adapter for public 591 listing-detail availability checks."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SaleStatusVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ListingStatusCheck:
    id: int
    source_property_id: str
    url: str


class CapturedSaleStatusVerifier:
    def __init__(
        self,
        script_path: str | Path,
        *,
        python_executable: str = sys.executable,
        timeout_seconds: float = 60 * 60,
    ) -> None:
        self.script_path = Path(script_path)
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds

    async def verify(
        self,
        items: list[ListingStatusCheck],
    ) -> dict[int, str]:
        if not items:
            return {}
        if not self.script_path.is_file():
            raise SaleStatusVerificationError(
                f"sale capture script not found: {self.script_path}"
            )

        with tempfile.TemporaryDirectory(prefix="radar-sale-status-") as temp_dir:
            temp_root = Path(temp_dir)
            input_path = temp_root / "input.json"
            output_path = temp_root / "output.json"
            input_path.write_text(
                json.dumps(
                    [
                        {
                            "id": item.id,
                            "source_property_id": item.source_property_id,
                            "url": item.url,
                        }
                        for item in items
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            process = await asyncio.create_subprocess_exec(
                self.python_executable,
                str(self.script_path),
                "--verify-input",
                str(input_path),
                "--verify-output",
                str(output_path),
                "--headless",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    self.timeout_seconds,
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise SaleStatusVerificationError(
                    f"sale status verification exceeded {self.timeout_seconds:g} seconds"
                ) from exc
            if process.returncode != 0:
                detail = (
                    stderr.decode("utf-8", errors="replace").strip()
                    or stdout.decode("utf-8", errors="replace").strip()
                )
                raise SaleStatusVerificationError(
                    "sale status verification exited with status "
                    f"{process.returncode}: {detail[-2000:]}"
                )
            if not output_path.is_file():
                raise SaleStatusVerificationError(
                    "sale status verification produced no output"
                )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        policy = payload.get("policy", {})
        if (
            not isinstance(policy, dict)
            or policy.get("public_pages_only") is not True
            or policy.get("login_used") is not False
            or policy.get("access_control_bypassed") is not False
        ):
            raise SaleStatusVerificationError(
                "status output does not satisfy the public-page policy"
            )
        results: dict[int, str] = {}
        for row in payload.get("results", []):
            if not isinstance(row, dict):
                continue
            try:
                database_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            status = str(row.get("status", "unknown"))
            results[database_id] = (
                status if status in {"active", "inactive"} else "unknown"
            )
        return results
