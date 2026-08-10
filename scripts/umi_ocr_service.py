"""Lifecycle and health checks for the local Umi-OCR HTTP service."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

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


def umi_ocr_process_counts(executable: str | Path) -> tuple[int, int]:
    if os.name != "nt":
        return (1, 1)
    executable = Path(executable).resolve()
    root = executable.parent.resolve()
    exe_value = str(executable).replace("'", "''")
    paddle_pattern = str(root / "UmiOCR-data" / "plugins" / "*" / "PaddleOCR-json.exe")
    paddle_value = paddle_pattern.replace("'", "''")
    script = (
        f"$umi = '{exe_value}'; $paddle = '{paddle_value}'; "
        "$rows = Get-CimInstance Win32_Process; "
        "$result = @{ umi = @($rows | Where-Object { $_.ExecutablePath -eq $umi }).Count; "
        "paddle = @($rows | Where-Object { $_.ExecutablePath -like $paddle }).Count }; "
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
    counts = json.loads(result.stdout.strip().lstrip("\ufeff"))
    return int(counts["umi"]), int(counts["paddle"])


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
