from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.config import get_settings
from apps.services.system_alerts import update_system_alert


RUNNING_TASKS = (
    "Property Case Radar Scheduler",
    "Property Case Radar OpenAB Gateway",
    "Property Case Radar OpenAB Sidecar",
)


def tcp_ready(host: str, port: int, timeout: float = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_service_ready(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except OSError:
        return False


def windows_task_states() -> dict[str, str]:
    names = ",".join(f"'{name}'" for name in RUNNING_TASKS)
    command = (
        f"Get-ScheduledTask -TaskName {names} -ErrorAction SilentlyContinue | "
        "Select-Object TaskName,State | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    if not result.stdout.strip():
        return {}
    payload = json.loads(result.stdout.lstrip("\ufeff"))
    rows = payload if isinstance(payload, list) else [payload]
    return {str(row["TaskName"]): str(row["State"]) for row in rows}


def latest_backup_is_fresh(directory: Path, *, maximum_age_hours: int = 28) -> bool:
    backups = list(directory.glob("radar_*.dump"))
    if not backups:
        return False
    newest = max(backups, key=lambda path: path.stat().st_mtime)
    modified = datetime.fromtimestamp(newest.stat().st_mtime, UTC)
    return modified >= datetime.now(UTC) - timedelta(hours=maximum_age_hours)


def run_watchdog() -> int:
    settings = get_settings()
    checks: list[tuple[str, str, bool, str]] = []

    try:
        task_states = windows_task_states()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        task_states = {}
        checks.append(
            ("watchdog:task-query", "Windows 排程狀態", False, f"無法讀取排程：{error}")
        )
    else:
        checks.append(
            ("watchdog:task-query", "Windows 排程狀態", True, "排程狀態可正常讀取")
        )

    for name in RUNNING_TASKS:
        # PowerShell's ConvertTo-Json serializes the ScheduledTask State enum
        # as either its label or numeric value depending on the host version.
        running = task_states.get(name) in {"Running", "4"}
        checks.append(
            (
                f"watchdog:task:{name}",
                name,
                running,
                f"目前狀態：{task_states.get(name, '找不到排程')}",
            )
        )

    checks.extend(
        [
            (
                "watchdog:openab-sidecar",
                "OpenAB 查詢 Sidecar",
                tcp_ready("127.0.0.1", 18765),
                "127.0.0.1:18765 TCP 健康檢查",
            ),
            (
                "watchdog:openab-pdf-broker",
                "OpenAB PDF Broker",
                http_service_ready("http://127.0.0.1:18766/health"),
                "http://127.0.0.1:18766/health",
            ),
            (
                "watchdog:umi-ocr",
                "Umi-OCR API",
                http_service_ready(settings.auction_capture_ocr_url),
                settings.auction_capture_ocr_url,
            ),
            (
                "watchdog:database-backup",
                "PostgreSQL 每日備份",
                latest_backup_is_fresh(settings.database_backup_dir),
                f"最近 28 小時內應有已驗證備份：{settings.database_backup_dir}",
            ),
        ]
    )

    failing = 0
    for key, title, healthy, detail in checks:
        if not healthy:
            failing += 1
        update_system_alert(
            settings,
            key=key,
            failing=not healthy,
            title=title,
            detail=detail,
        )
    print(f"watchdog finished: checks={len(checks)} failing={failing}")
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(run_watchdog())
