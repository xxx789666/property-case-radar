"""Lifecycle and health checks for the local Umi-OCR HTTP service."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# A valid 1x1 PNG. Unlike the old empty-base64 check, this forces Umi-OCR to
# start the PaddleOCR engine and exercise the same model-switch path used by
# CAPTCHA recognition.
PROBE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAXklEQVR4nO3YsQnAUAwD"
    "0fOR/Vd2JkiV4ucgr1VjUCHw7C5lEidxEidxEidxEnc9BTPDl+zDXuUbkDiJkziJkziJk"
    "ziJkziJkziJkziJm/8vdJjESZzESZzEefqAt26gCwk7KbHw7gAAAABJRU5ErkJggg=="
)


def _patch_text_file(path: Path, old: str, new: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return False
    backup = path.with_suffix(f"{path.suffix}.radar-cp950.bak")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(text.replace(old, new, 1), encoding="utf-8")
    temporary.replace(path)
    return True


def ensure_umi_ocr_cp950_compatibility(executable: str | Path) -> int:
    """Apply idempotent safeguards to upstream prints that crash on cp950."""

    root = Path(executable).resolve().parent / "UmiOCR-data"
    ppo_api = (
        root
        / "plugins"
        / "win7_x64_PaddleOCR-json"
        / "PPOCR_api.py"
    )
    cmd_client = root / "py_src" / "server" / "cmd_client.py"
    changes = 0
    changes += int(
        _patch_text_file(
            ppo_api,
            '        print("###  PPOCR引擎子进程关闭！")',
            '        print("### PPOCR engine subprocess closed.")',
        )
    )
    changes += int(
        _patch_text_file(
            cmd_client,
            "    print(res)\n    _output(argv, \"-->\", \"w\", res)",
            (
                "    try:\n"
                "        print(res)\n"
                "    except UnicodeEncodeError:\n"
                "        encoding = getattr(sys.stdout, \"encoding\", None) or \"utf-8\"\n"
                "        print(str(res).encode(encoding, errors=\"replace\").decode(encoding))\n"
                "    _output(argv, \"-->\", \"w\", res)"
            ),
        )
    )
    return changes


def umi_ocr_process_details(executable: str | Path) -> tuple[list[int], list[int]]:
    if os.name != "nt":
        return ([os.getpid()], [os.getpid()])
    executable = Path(executable).resolve()
    root = executable.parent.resolve()
    exe_value = str(executable).replace("'", "''")
    paddle_pattern = str(root / "UmiOCR-data" / "plugins" / "*" / "PaddleOCR-json.exe")
    paddle_value = paddle_pattern.replace("'", "''")
    script = (
        f"$umi = '{exe_value}'; $paddle = '{paddle_value}'; "
        "$rows = Get-CimInstance Win32_Process; "
        "$result = @{ umi = @($rows | Where-Object { $_.ExecutablePath -eq $umi } | "
        "ForEach-Object { $_.ProcessId }); "
        "paddle = @($rows | Where-Object { $_.ExecutablePath -like $paddle } | "
        "ForEach-Object { $_.ProcessId }) }; "
        "$result | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=True,
    )
    details = json.loads(result.stdout.strip().lstrip("\ufeff"))

    def process_ids(value: object) -> list[int]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [int(process_id) for process_id in values]

    return process_ids(details["umi"]), process_ids(details["paddle"])


def umi_ocr_process_counts(executable: str | Path) -> tuple[int, int]:
    umi_ids, paddle_ids = umi_ocr_process_details(executable)
    return len(umi_ids), len(paddle_ids)


def umi_ocr_liveness(
    url: str,
    *,
    executable: str | Path,
    timeout: float = 2,
    max_paddle_processes: int = 4,
) -> bool:
    """Check Umi without submitting an OCR job that can spawn a new worker."""

    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
        umi_count, paddle_count = umi_ocr_process_counts(executable)
        # Zero Paddle workers is the normal idle state before the first OCR
        # request (and immediately after a restart).  The Umi listener is the
        # service; Paddle workers are created lazily and only an excessive
        # count indicates the leak this check is intended to catch.
        return umi_count == 1 and 0 <= paddle_count <= max_paddle_processes
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def umi_ocr_diagnostic(
    url: str,
    *,
    executable: str | Path,
    task_state: str,
    log_path: str | Path | None = None,
) -> str:
    """Return concise process, port, task, and recent startup diagnostics."""

    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    port_ready = False
    try:
        if parsed.hostname:
            with socket.create_connection((parsed.hostname, port), timeout=2):
                port_ready = True
    except OSError:
        pass

    try:
        umi_ids, paddle_ids = umi_ocr_process_details(executable)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        process_detail = f"程序查詢失敗={error}"
    else:
        process_detail = (
            f"Umi程序={len(umi_ids)}, Paddle程序={len(paddle_ids)}, "
            f"PID={umi_ids or '-'}, PaddlePID={paddle_ids or '-'}"
        )

    parts = [
        f"排程={task_state}",
        process_detail,
        f"連接埠{port}={'監聽中' if port_ready else '未監聽'}",
    ]
    if log_path is not None:
        log_path = Path(log_path)
        if log_path.is_dir():
            try:
                log_path = max(
                    log_path.glob("log_*.jsonl.txt"),
                    key=lambda path: path.stat().st_mtime,
                )
            except (ValueError, OSError):
                log_path = Path("")
        try:
            is_recent = time.time() - log_path.stat().st_mtime <= 3600
            lines = (
                log_path.read_text(encoding="utf-8-sig").splitlines()
                if is_recent
                else []
            )
        except OSError:
            lines = []
        if lines:
            parts.append("最近紀錄=" + " | ".join(lines[-3:])[-900:])
    return "；".join(parts)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "--ensure-compatibility":
        raise SystemExit(
            "usage: umi_ocr_service.py --ensure-compatibility <Umi-OCR.exe>"
        )
    print(ensure_umi_ocr_cp950_compatibility(sys.argv[2]))


def umi_ocr_ready(
    url: str,
    timeout: float = 10,
    *,
    executable: str | Path | None = None,
) -> bool:
    payload = json.dumps(
        {
            "base64": PROBE_PNG_BASE64,
            "options": {
                "ocr.language": "models/config_en.txt",
                "data.format": "text",
                "tbpu.parser": "single_none",
                "ocr.cls": False,
                "ocr.limit_side_len": 960,
            },
        },
        ensure_ascii=True,
    ).encode("ascii")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            result = json.loads(response.read().decode("utf-8"))
        response_ok = isinstance(result, dict) and result.get("code") in {100, 101}
        if not response_ok:
            return False
        if executable is not None:
            umi_count, paddle_count = umi_ocr_process_counts(executable)
            # Umi may retain more than one Paddle worker after switching OCR
            # models. The successful API response above is authoritative;
            # extra healthy workers must not trigger a restart loop.
            if umi_count != 1 or paddle_count < 1:
                return False
        return True
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        urllib.error.HTTPError,
    ):
        return False


def terminate_umi_ocr_processes(executable: str | Path) -> None:
    """Stop only Umi-OCR and its PaddleOCR child below the configured root."""

    if os.name != "nt":
        return
    executable = Path(executable).resolve()
    root = executable.parent.resolve()
    exe_value = str(executable).replace("'", "''")
    paddle_pattern = str(root / "UmiOCR-data" / "plugins" / "*" / "PaddleOCR-json.exe")
    paddle_value = paddle_pattern.replace("'", "''")
    script = (
        f"$umi = '{exe_value}'; $paddle = '{paddle_value}'; "
        "$targets = Get-CimInstance Win32_Process | Where-Object { "
        "$_.ExecutablePath -eq $umi -or $_.ExecutablePath -like $paddle }; "
        "$targets | Sort-Object { if ($_.ExecutablePath -eq $umi) { 1 } else { 0 } } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };"
    )
    subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    time.sleep(2)


def launch_umi_ocr(executable: str | Path) -> subprocess.Popen[bytes]:
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Umi-OCR executable not found: {executable}")
    ensure_umi_ocr_cp950_compatibility(executable)

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    # PYTHONIOENCODING is required in addition to PYTHONUTF8. Umi-OCR's
    # bundled Python otherwise opens stdout as cp950 and crashes when its
    # Simplified-Chinese diagnostics contain characters such as 进/运.
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startupinfo,
        creationflags=creationflags,
        close_fds=True,
        env=environment,
    )


def restart_umi_ocr(executable: str | Path) -> None:
    terminate_umi_ocr_processes(executable)
    launch_umi_ocr(executable)
