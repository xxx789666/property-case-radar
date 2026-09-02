import json
import re
import subprocess
import sys
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


def test_run_postgres_launcher_is_postgres_only():
    launcher = REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")

    assert "use_d_runtime" not in launcher_text
    assert "python" not in launcher_text.lower()
    assert "node" not in launcher_text.lower()
    assert ".runtime" not in launcher_text
    assert r"D:\PostgreSQL\17\bin" in launcher_text
    assert r"D:\PostgreSQL\17\data" in launcher_text
    assert "15432" in launcher_text
    assert "pg_ctl" in launcher_text
    assert "status" in launcher_text
    assert "start" in launcher_text
    assert "Test-Path" in launcher_text
    assert "-w" in launcher_text
    assert "-t 60" in launcher_text or '"-t", "60"' in launcher_text
    assert "postgres-local.log" in launcher_text


def test_run_postgres_start_detaches_native_streams_from_outer_capture():
    launcher_text = (
        REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    ).read_text(encoding="utf-8")

    assert "& $pgCtl start" not in launcher_text
    assert "Start-Process" in launcher_text
    assert "-FilePath $pgCtl" in launcher_text
    assert "ArgumentList" in launcher_text
    assert '"-p {0}"' in launcher_text
    assert '''"-D", ('"{0}"' -f ($data -replace '"', '\\"'))''' in launcher_text
    assert '''"-l", ('"{0}"' -f ($logPath -replace '"', '\\"'))''' in launcher_text
    assert "RedirectStandardOutput" in launcher_text
    assert "RedirectStandardError" in launcher_text
    assert "postgres-self-heal-ctl.out.log" in launcher_text
    assert "postgres-self-heal-ctl.err.log" in launcher_text
    assert "WaitForExit" in launcher_text
    assert "70000" in launcher_text
    assert "ComSpec" not in launcher_text
    assert "startCommand" not in launcher_text
    assert "/c" not in launcher_text
    assert "2>&1" not in launcher_text
    assert "WindowStyle" not in launcher_text
    assert re.search(r"^\s*-Wait\b", launcher_text, re.M) is None


def test_run_postgres_failure_rethrows_control_log_or_fallback():
    launcher_text = (
        REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    ).read_text(encoding="utf-8")

    assert "Get-Content" in launcher_text
    assert "$stderrDetail" in launcher_text
    assert "$stdoutDetail" in launcher_text
    assert launcher_text.find("$stderrDetail") < launcher_text.find("$stdoutDetail")
    assert "postgres-self-heal-ctl.err.log" in launcher_text
    assert "postgres-self-heal-ctl.out.log" in launcher_text
    assert "PostgreSQL startup failed" in launcher_text
    assert "timed out" in launcher_text.lower()
    assert "no control log output" in launcher_text
    assert "$exitCode" in launcher_text
    assert "HasExited" in launcher_text
    assert "Kill()" in launcher_text


def _win32_argument_list_snippet():
    launcher_text = (
        REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"\$argumentList = @\((.*?)\)\s*\$process = Start-Process",
        launcher_text,
        re.S,
    )
    assert match, "run_postgres.ps1 must build $argumentList for Start-Process"
    return match.group(1)


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_start_process_keeps_spaced_data_and_log_paths_as_one_argv(tmp_path):
    printer = tmp_path / "print_argv.py"
    printer.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="ascii",
    )
    stdout_log = tmp_path / "argv-out.log"
    stderr_log = tmp_path / "argv-err.log"
    data = str(tmp_path / "Program Files" / "pg data")
    log_path = str(tmp_path / "My Logs" / "postgres-local.log")
    snippet = _win32_argument_list_snippet()
    command = f"""
$ErrorActionPreference = 'Stop'
$data = {_ps_single_quote(data)}
$logPath = {_ps_single_quote(log_path)}
$port = '15432'
$argumentList = @(
    ('"{{0}}"' -f ({_ps_single_quote(str(printer))} -replace '"', '\\"')),
    {snippet}
)
$process = Start-Process -FilePath {_ps_single_quote(sys.executable)} `
    -ArgumentList $argumentList `
    -PassThru `
    -NoNewWindow `
    -RedirectStandardOutput {_ps_single_quote(str(stdout_log))} `
    -RedirectStandardError {_ps_single_quote(str(stderr_log))}
$null = $process.Handle
if (-not $process.WaitForExit(30000)) {{
    if (-not $process.HasExited) {{ $process.Kill() }}
    throw 'argv probe timed out'
}}
if ($process.ExitCode -ne 0) {{
    throw "argv probe exited $($process.ExitCode)"
}}
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    argv = json.loads(stdout_log.read_text(encoding="utf-8").strip())
    assert argv[argv.index("-D") + 1] == data
    assert argv[argv.index("-l") + 1] == log_path
    assert argv[argv.index("-o") + 1] == "-p 15432"


def test_powershell_uses_execution_policy_bypass(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr("scripts.scheduler_self_healer.subprocess.run", fake_run)

    assert powershell("Write-Output ok") == "ok"
    assert "-NonInteractive" in captured["args"]
    policy_index = captured["args"].index("-ExecutionPolicy")
    assert captured["args"][policy_index + 1] == "Bypass"


def test_postgres_scripts_parse_without_executing():
    path = REPOSITORY_ROOT / "scripts" / "run_postgres.ps1"
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
