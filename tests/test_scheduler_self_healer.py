import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from apps.config import Settings
from scripts.scheduler_self_healer import (
    REPOSITORY_ROOT,
    heal_component,
    powershell,
    python_script_running,
    reserve_attempt,
    start_postgres,
)


def make_settings(tmp_path, **overrides):
    return Settings(
        database_url="sqlite://",
        discord_token=None,
        self_heal_state_path=tmp_path / "self-heal.json",
        self_heal_service_cooldown_minutes=10,
        self_heal_service_attempt_window_minutes=60,
        self_heal_max_retries=3,
        **overrides,
    )


def test_attempt_budget_enforces_cooldown_and_limit(tmp_path):
    settings = make_settings(tmp_path)
    state = {}
    now = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)

    assert reserve_attempt(state, "scheduler", settings, now=now) == 1
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=5),
        )
        is None
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=10),
        )
        == 2
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=20),
        )
        == 3
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=30),
        )
        is None
    )


def test_successful_repair_clears_attempt_state(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    state = {}
    ready = False
    notifications = []
    alerts = []

    def repair():
        nonlocal ready
        ready = True

    monkeypatch.setattr(
        "scripts.scheduler_self_healer.send_discord_system_message",
        lambda _settings, message: notifications.append(message) or True,
    )
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )

    healthy, attempted = heal_component(
        settings=settings,
        state=state,
        key="scheduler",
        title="Scheduler",
        healthy=lambda: ready,
        repair=repair,
        wait_seconds=1,
        now=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
    )

    assert healthy
    assert attempted
    assert "scheduler" not in state
    assert "已恢復" in notifications[0]
    assert alerts[-1]["failing"] is False


def test_python_script_running_filters_to_python_process(monkeypatch):
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.powershell",
        lambda script, **_kwargs: (
            "True" if "capture_auction_results.py" in script else "False"
        ),
    )

    assert python_script_running("D:/capture_auction_results.py")


def test_failed_repair_notification_includes_component_diagnostic(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    notifications = []
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.wait_until",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.send_discord_system_message",
        lambda _settings, message: notifications.append(message) or True,
    )

    healthy, attempted = heal_component(
        settings=settings,
        state={},
        key="umi-ocr",
        title="Umi-OCR API",
        healthy=lambda: False,
        repair=lambda: None,
        diagnostic=lambda: "排程=Ready；Umi程序=0；連接埠1224=未監聽",
        wait_seconds=1,
        now=datetime(2026, 8, 20, 1, 0, tzinfo=UTC),
    )

    assert not healthy
    assert attempted
    assert "排程=Ready" in notifications[0]
    assert "連接埠1224=未監聽" in notifications[0]


def test_start_postgres_invokes_run_postgres_script(monkeypatch):
    captured = {}

    def fake_powershell(script, *, timeout=30):
        captured["script"] = script
        captured["timeout"] = timeout
        return ""

    monkeypatch.setattr("scripts.scheduler_self_healer.powershell", fake_powershell)

    start_postgres()

    script_path = REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    escaped = str(script_path).replace("'", "''")
    assert script_path.is_file()
    assert f"& '{escaped}'" in captured["script"]
    assert "Win32_Service" not in captured["script"]
    assert "postgresql-x64-17" not in captured["script"]
    assert captured["timeout"] >= 60


def test_start_postgres_escapes_single_quotes_in_script_path(monkeypatch, tmp_path):
    captured = {}
    repo_root = tmp_path / "repo's root"

    def fake_powershell(script, *, timeout=30):
        captured["script"] = script
        captured["timeout"] = timeout
        return ""

    monkeypatch.setattr("scripts.scheduler_self_healer.REPOSITORY_ROOT", repo_root)
    monkeypatch.setattr("scripts.scheduler_self_healer.powershell", fake_powershell)

    start_postgres()

    escaped = str(repo_root / "scripts" / "run_postgres.ps1").replace("'", "''")
    assert f"& '{escaped}'" in captured["script"]
    assert "repo''s root" in captured["script"]


def _fake_powershell_run(*, returncode, stdout="", stderr=""):
    def fake_run(args, **kwargs):
        completed = subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if kwargs.get("check") and completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode,
                args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed

    return fake_run


def test_powershell_nonzero_exit_preserves_stderr_and_exit_code(monkeypatch):
    long_script = "Write-Error " + ("A" * 400)
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.subprocess.run",
        _fake_powershell_run(
            returncode=17,
            stderr="pg_ctl: directory does not exist\n",
        ),
    )

    with pytest.raises(subprocess.SubprocessError) as excinfo:
        powershell(long_script)

    message = str(excinfo.value)
    assert "17" in message
    assert "pg_ctl: directory does not exist" in message
    assert "A" * 50 not in message


def test_powershell_nonzero_exit_falls_back_to_stdout(monkeypatch):
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.subprocess.run",
        _fake_powershell_run(
            returncode=3,
            stdout="The term 'pg_ctl.exe' is not recognized\n",
            stderr="   \n",
        ),
    )

    with pytest.raises(subprocess.SubprocessError) as excinfo:
        powershell("Get-Command pg_ctl.exe")

    message = str(excinfo.value)
    assert "3" in message
    assert "The term 'pg_ctl.exe' is not recognized" in message


def test_heal_component_failure_keeps_stderr_despite_long_command(
    monkeypatch, tmp_path
):
    settings = make_settings(tmp_path)
    notifications = []
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.subprocess.run",
        _fake_powershell_run(
            returncode=1,
            stderr="pg_ctl: PID file is missing\n",
        ),
    )
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.send_discord_system_message",
        lambda _settings, message: notifications.append(message) or True,
    )

    healthy, attempted = heal_component(
        settings=settings,
        state={},
        key="postgresql",
        title="PostgreSQL",
        healthy=lambda: False,
        repair=lambda: powershell("x" * 600),
        wait_seconds=1,
        now=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
    )

    assert not healthy
    assert attempted
    assert notifications
    assert "pg_ctl: PID file is missing" in notifications[0]
    assert "1" in notifications[0]
    assert "x" * 50 not in notifications[0]


def test_run_postgres_launcher_exists_and_uses_d_runtime():
    launcher = REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    runtime = REPOSITORY_ROOT / "scripts" / "use_d_runtime.ps1"
    assert launcher.is_file()
    assert runtime.is_file()

    launcher_text = launcher.read_text(encoding="utf-8")
    runtime_text = runtime.read_text(encoding="utf-8")

    assert "use_d_runtime.ps1" in launcher_text
    assert "pg_ctl" in launcher_text
    assert "status" in launcher_text
    assert "start" in launcher_text
    assert "$RadarPostgresData" in launcher_text
    assert "$RadarPostgresPort" in launcher_text

    assert 'D:\\PostgreSQL\\17\\bin' in runtime_text
    assert 'D:\\PostgreSQL\\17\\data' in runtime_text
    assert "15432" in runtime_text
    assert "Test-Path" in runtime_text
    assert "pg_ctl.exe" in runtime_text
    assert "$RadarPostgresData" in runtime_text
    assert "$RadarPostgresPort" in runtime_text


def test_postgres_scripts_parse_without_executing():
    for name in ("run_postgres.ps1", "use_d_runtime.ps1"):
        path = REPOSITORY_ROOT / "scripts" / name
        escaped = str(path).replace("'", "''")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$parseErrors = $null; "
                    "[void][System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{escaped}', [ref]$null, [ref]$parseErrors); "
                    "if ($parseErrors) { $parseErrors | ForEach-Object { $_.ToString() }; exit 1 }"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, result.stderr or result.stdout
