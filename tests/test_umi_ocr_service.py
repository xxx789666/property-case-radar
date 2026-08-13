import json
from pathlib import Path

from scripts.umi_ocr_service import (
    ensure_umi_ocr_cp950_compatibility,
    launch_umi_ocr,
    umi_ocr_liveness,
    umi_ocr_ready,
)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({"code": 101, "data": ""}).encode("utf-8")


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_liveness_does_not_submit_ocr_and_rejects_worker_leak(monkeypatch):
    connections = []
    monkeypatch.setattr(
        "scripts.umi_ocr_service.socket.create_connection",
        lambda address, timeout: connections.append((address, timeout)) or FakeSocket(),
    )
    monkeypatch.setattr(
        "scripts.umi_ocr_service.umi_ocr_process_counts",
        lambda _executable: (1, 169),
    )

    assert not umi_ocr_liveness(
        "http://127.0.0.1:1224/api/ocr",
        executable="Umi-OCR.exe",
    )
    assert connections == [(('127.0.0.1', 1224), 2)]


def test_liveness_accepts_one_healthy_worker(monkeypatch):
    monkeypatch.setattr(
        "scripts.umi_ocr_service.socket.create_connection",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    monkeypatch.setattr(
        "scripts.umi_ocr_service.umi_ocr_process_counts",
        lambda _executable: (1, 1),
    )

    assert umi_ocr_liveness(
        "http://127.0.0.1:1224/api/ocr",
        executable="Umi-OCR.exe",
    )


def test_liveness_accepts_idle_service_before_first_ocr_request(monkeypatch):
    monkeypatch.setattr(
        "scripts.umi_ocr_service.socket.create_connection",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    monkeypatch.setattr(
        "scripts.umi_ocr_service.umi_ocr_process_counts",
        lambda _executable: (1, 0),
    )

    assert umi_ocr_liveness(
        "http://127.0.0.1:1224/api/ocr",
        executable="Umi-OCR.exe",
    )


def test_health_probe_uses_real_image_and_english_model(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("ascii"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("scripts.umi_ocr_service.urllib.request.urlopen", fake_urlopen)

    assert umi_ocr_ready("http://127.0.0.1:1224/api/ocr", timeout=7)
    assert captured["body"]["base64"]
    assert captured["body"]["options"]["ocr.language"] == "models/config_en.txt"
    assert captured["timeout"] == 7


def test_health_probe_accepts_multiple_paddle_workers(monkeypatch):
    monkeypatch.setattr(
        "scripts.umi_ocr_service.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        "scripts.umi_ocr_service.umi_ocr_process_counts",
        lambda _executable: (1, 2),
    )

    assert umi_ocr_ready(
        "http://127.0.0.1:1224/api/ocr",
        executable="Umi-OCR.exe",
    )


def test_launch_forces_utf8_environment(monkeypatch, tmp_path: Path):
    executable = tmp_path / "Umi-OCR.exe"
    executable.write_bytes(b"")
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("scripts.umi_ocr_service.subprocess.Popen", fake_popen)

    launch_umi_ocr(executable)

    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["stdout"] is not None
    assert captured["stderr"] is not None


def test_cp950_patch_is_idempotent_and_keeps_backups(tmp_path: Path):
    executable = tmp_path / "Umi-OCR.exe"
    executable.write_bytes(b"")
    ppo = (
        tmp_path
        / "UmiOCR-data"
        / "plugins"
        / "win7_x64_PaddleOCR-json"
        / "PPOCR_api.py"
    )
    cmd = tmp_path / "UmiOCR-data" / "py_src" / "server" / "cmd_client.py"
    ppo.parent.mkdir(parents=True)
    cmd.parent.mkdir(parents=True)
    ppo.write_text('        print("###  PPOCR引擎子进程关闭！")\n', encoding="utf-8")
    cmd.write_text(
        '    print(res)\n    _output(argv, "-->", "w", res)\n',
        encoding="utf-8",
    )

    assert ensure_umi_ocr_cp950_compatibility(executable) == 2
    assert ensure_umi_ocr_cp950_compatibility(executable) == 0
    assert "PPOCR engine subprocess closed" in ppo.read_text(encoding="utf-8")
    assert "except UnicodeEncodeError" in cmd.read_text(encoding="utf-8")
    assert ppo.with_suffix(".py.radar-cp950.bak").is_file()
    assert cmd.with_suffix(".py.radar-cp950.bak").is_file()
