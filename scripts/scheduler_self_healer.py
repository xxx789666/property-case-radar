#!/usr/bin/env python3
"""Backstop recovery for Radar's Windows tasks and local services."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.config import Settings, get_settings
from apps.services.system_alerts import (
    send_discord_system_message,
    update_system_alert,
)
from scripts.system_watchdog import latest_backup_is_fresh
from scripts.umi_ocr_service import (
    umi_ocr_diagnostic,
    umi_ocr_liveness,
)

SCHEDULER_TASK = "Property Case Radar Scheduler"
GATEWAY_TASK = "Property Case Radar OpenAB Gateway"
SIDECAR_TASK = "Property Case Radar OpenAB Sidecar"
BACKUP_TASK = "Property Case Radar Database Backup"
UMI_OCR_TASK = "Property Case Radar Umi-OCR"
RUN_POSTGRES_TIMEOUT_SECONDS = 90


def tcp_ready(host: str, port: int, timeout: float = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ready(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except OSError:
        return False


class PowerShellCommandError(subprocess.CalledProcessError):
    """PowerShell failure whose message prefers captured stderr over the command."""

    def __str__(self) -> str:
        detail = _captured_process_text(self.stderr) or _captured_process_text(
            self.output
        )
        if detail:
            return f"PowerShell exited with {self.returncode}: {detail}"
        return f"PowerShell exited with {self.returncode}"


def _captured_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return value.strip()


def powershell(script: str, *, timeout: int = 30) -> str:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise PowerShellCommandError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout.strip().lstrip("\ufeff")


def windows_task_state(name: str) -> str:
    escaped = name.replace("'", "''")
    return powershell(
        f"$task = Get-ScheduledTask -TaskName '{escaped}' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { 'Missing' } else { $task.State.ToString() }"
    )


def windows_task_diagnostic(name: str) -> str:
    escaped = name.replace("'", "''")
    return powershell(
        f"$task = Get-ScheduledTask -TaskName '{escaped}' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { 'Missing' } else { "
        f"$info = Get-ScheduledTaskInfo -TaskName '{escaped}'; "
        "$result = '0x{0:X8}' -f ($info.LastTaskResult -band 0xffffffff); "
        "'{0}, last_result={1}, last_run={2:yyyy-MM-dd HH:mm:ss}' "
        "-f $task.State, $result, $info.LastRunTime }"
    )


def python_script_running(script_path: str | Path) -> bool:
    filename = Path(script_path).name.replace("'", "''")
    return powershell(
        "$process = Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -like 'python*.exe' -and "
        f"$_.CommandLine -like '*{filename}*' "
        "} | Select-Object -First 1; "
        "if ($null -eq $process) { 'False' } else { 'True' }"
    ) == "True"


def restart_windows_task(name: str) -> None:
    escaped = name.replace("'", "''")
    powershell(
        f"$task = Get-ScheduledTask -TaskName '{escaped}' -ErrorAction Stop; "
        f"Stop-ScheduledTask -TaskName '{escaped}' -ErrorAction SilentlyContinue; "
        "Start-Sleep -Seconds 2; "
        f"Start-ScheduledTask -TaskName '{escaped}'",
        timeout=45,
    )


def start_windows_task(name: str) -> None:
    escaped = name.replace("'", "''")
    powershell(
        f"Start-ScheduledTask -TaskName '{escaped}' -ErrorAction Stop",
        timeout=30,
    )


def start_postgres() -> None:
    """Start the D-drive PostgreSQL runtime via scripts/run_postgres.ps1."""

    script = REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    escaped = str(script).replace("'", "''")
    powershell(
        f"& '{escaped}'; if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}",
        timeout=RUN_POSTGRES_TIMEOUT_SECONDS,
    )


def wait_until(check: Callable[[], bool], *, timeout: int, interval: int = 3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return check()


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{process_id()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def process_id() -> int:
    import os

    return os.getpid()


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def reserve_attempt(
    state: dict,
    key: str,
    settings: Settings,
    *,
    now: datetime,
) -> int | None:
    record = state.setdefault(key, {})
    window = parse_time(record.get("window_started"))
    window_length = timedelta(minutes=settings.self_heal_service_attempt_window_minutes)
    if window is None or now - window >= window_length:
        record.clear()
        record["window_started"] = now.isoformat()
        record["attempts"] = 0

    last_attempt = parse_time(record.get("last_attempt"))
    cooldown = timedelta(minutes=settings.self_heal_service_cooldown_minutes)
    if last_attempt is not None and now - last_attempt < cooldown:
        return None

    attempts = int(record.get("attempts", 0))
    if attempts >= settings.self_heal_max_retries:
        return None
    attempts += 1
    record["attempts"] = attempts
    record["last_attempt"] = now.isoformat()
    return attempts


def clear_attempts(state: dict, key: str) -> None:
    state.pop(key, None)


def heal_component(
    *,
    settings: Settings,
    state: dict,
    key: str,
    title: str,
    healthy: Callable[[], bool],
    repair: Callable[[], None],
    diagnostic: Callable[[], str] | None = None,
    wait_seconds: int,
    now: datetime,
) -> tuple[bool, bool]:
    """Return ``(healthy_after_check, action_attempted)``."""

    exhausted_key = f"self-heal-exhausted:{key}"
    initially_healthy = healthy()
    print(f"component {key}: healthy_before={initially_healthy}")
    if diagnostic is not None:
        try:
            print(f"component {key}: diagnostic_before={diagnostic().strip()}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"component {key}: diagnostic_before_failed={error}")
    if initially_healthy:
        clear_attempts(state, key)
        update_system_alert(
            settings,
            key=exhausted_key,
            failing=False,
            title=f"自癒系統 {title}",
            detail=f"{title} 已恢復正常。",
        )
        return True, False

    attempt = reserve_attempt(state, key, settings, now=now)
    if attempt is None:
        record = state.get(key, {})
        exhausted = int(record.get("attempts", 0)) >= settings.self_heal_max_retries
        if exhausted:
            update_system_alert(
                settings,
                key=exhausted_key,
                failing=True,
                title=f"自癒系統 {title}",
                detail=(
                    f"{title} 在 "
                    f"{settings.self_heal_service_attempt_window_minutes} 分鐘內已嘗試 "
                    f"{settings.self_heal_max_retries} 次，仍未恢復，需要人工處理。"
                ),
            )
        return False, False

    try:
        repair()
        recovered = wait_until(healthy, timeout=wait_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        recovered = False
        failure_detail = str(error)[:500]
    else:
        failure_detail = "健康檢查已通過" if recovered else "健康檢查仍未通過"

    if not recovered and diagnostic is not None:
        try:
            detail = diagnostic().strip()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = f"診斷資料取得失敗：{error}"
        if detail:
            failure_detail = f"{failure_detail}；{detail}"[:1800]
    print(
        f"component {key}: repair_attempt={attempt} recovered={recovered} "
        f"detail={failure_detail}"
    )

    if recovered:
        clear_attempts(state, key)
        send_discord_system_message(
            settings,
            (
                f"🛠️ **排程自癒 {title}｜已恢復**\n"
                f"第 {attempt} 次修復成功；{failure_detail}。"
            ),
        )
        update_system_alert(
            settings,
            key=exhausted_key,
            failing=False,
            title=f"自癒系統 {title}",
            detail=f"{title} 已恢復正常。",
        )
        return True, True

    if attempt >= settings.self_heal_max_retries:
        update_system_alert(
            settings,
            key=exhausted_key,
            failing=True,
            title=f"自癒系統 {title}",
            detail=(
                f"第 {attempt}/{settings.self_heal_max_retries} 次修復失敗："
                f"{failure_detail}。需要人工處理。"
            ),
        )
    else:
        send_discord_system_message(
            settings,
            (
                f"⚠️ **排程自癒 {title}｜第 {attempt} 次未成功**\n"
                f"{failure_detail}；將於冷卻 "
                f"{settings.self_heal_service_cooldown_minutes} 分鐘後再試。"
            ),
        )
    return False, True


def run_self_healer() -> int:
    settings = get_settings()
    state_path = settings.self_heal_state_path
    if not state_path.is_absolute():
        state_path = REPOSITORY_ROOT / state_path
    state = read_state(state_path)
    now = datetime.now(UTC)

    def scheduler_healthy() -> bool:
        return windows_task_state(SCHEDULER_TASK) == "Running"

    def gateway_healthy() -> bool:
        return (
            windows_task_state(GATEWAY_TASK) == "Running"
            and http_ready("http://127.0.0.1:18766/health")
        )

    def sidecar_healthy() -> bool:
        return (
            windows_task_state(SIDECAR_TASK) == "Running"
            and tcp_ready("127.0.0.1", 18765)
            and http_ready("http://127.0.0.1:18767/health")
        )

    def backup_healthy() -> bool:
        return latest_backup_is_fresh(settings.database_backup_dir)

    def postgres_healthy() -> bool:
        from sqlalchemy.engine import make_url

        url = make_url(settings.database_url)
        return tcp_ready(url.host or "127.0.0.1", url.port or 5432)

    def umi_healthy() -> bool:
        # The capture script deliberately switches among several OCR model
        # configurations and Umi retains their workers until the batch ends.
        # Do not interrupt an active county batch at the stricter idle limit;
        # after capture exits, the next check uses the idle limit and cleans
        # up any workers that did not converge.
        max_workers = (
            32 if python_script_running(settings.auction_capture_script) else 4
        )
        return umi_ocr_liveness(
            settings.auction_capture_ocr_url,
            executable=settings.auction_capture_ocr_executable,
            max_paddle_processes=max_workers,
        )

    components = (
        (
            "scheduler",
            "Scheduler",
            scheduler_healthy,
            lambda: restart_windows_task(SCHEDULER_TASK),
            30,
        ),
        (
            "openab-gateway",
            "OpenAB Gateway／PDF Broker",
            gateway_healthy,
            lambda: restart_windows_task(GATEWAY_TASK),
            60,
        ),
        (
            "openab-sidecar",
            "OpenAB Sidecar／訂閱 Broker",
            sidecar_healthy,
            lambda: restart_windows_task(SIDECAR_TASK),
            60,
        ),
        (
            "postgresql",
            "PostgreSQL",
            postgres_healthy,
            start_postgres,
            60,
        ),
        (
            "database-backup",
            "PostgreSQL 每日備份",
            backup_healthy,
            lambda: start_windows_task(BACKUP_TASK),
            180,
        ),
        (
            "umi-ocr",
            "Umi-OCR API",
            umi_healthy,
            lambda: restart_windows_task(UMI_OCR_TASK),
            int(settings.auction_capture_ocr_startup_timeout_seconds),
            lambda: umi_ocr_diagnostic(
                settings.auction_capture_ocr_url,
                executable=settings.auction_capture_ocr_executable,
                task_state=windows_task_diagnostic(UMI_OCR_TASK),
                log_path=(
                    Path(settings.auction_capture_ocr_executable).resolve().parent
                    / "UmiOCR-data"
                    / "logs"
                ),
            ),
        ),
    )

    failing = 0
    actions = 0
    for component in components:
        key, title, healthy, repair, wait_seconds, *extra = component
        is_healthy, attempted = heal_component(
            settings=settings,
            state=state,
            key=key,
            title=title,
            healthy=healthy,
            repair=repair,
            diagnostic=extra[0] if extra else None,
            wait_seconds=wait_seconds,
            now=now,
        )
        failing += int(not is_healthy)
        actions += int(attempted)
        write_state(state_path, state)

    print(
        f"self-healer finished: components={len(components)} "
        f"failing={failing} actions={actions}"
    )
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(run_self_healer())
