from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.radar_agent_pdf import _broker_endpoint, main

REPO_ROOT = Path(__file__).resolve().parent.parent


class _BrokerHandler(BaseHTTPRequestHandler):
    received: dict | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        type(self).received = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(
            {
                "ok": True,
                "message_id": "123",
                "message_url": "https://discord.com/channels/1/2/123",
                "filenames": ["1050100025324_1_1.pdf"],
                "attachments": [
                    {
                        "filename": "1050100025324_1_1.pdf",
                        "url": "https://cdn.discord.test/1050100025324_1_1.pdf",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _DiscordHandler(BaseHTTPRequestHandler):
    upload_body = b""

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "id": "1531235755677188116",
                "parent_id": "1530076529751756870",
                "guild_id": "1530072733818556538",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        type(self).upload_body = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(
            {
                "id": "1531240484385591447",
                "attachments": [
                    {
                        "filename": "1050100025324_1_1.pdf",
                        "url": "https://cdn.discord.test/1050100025324_1_1.pdf",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_broker_url_must_be_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_PDF_UPLOAD_BROKER_URL", "https://example.com")
    with pytest.raises(ValueError, match="loopback"):
        _broker_endpoint()


def test_upload_cli_sends_only_case_coordinates_to_loopback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "RADAR_PDF_UPLOAD_BROKER_URL",
        f"http://127.0.0.1:{server.server_address[1]}",
    )
    try:
        code = main(
            [
                "upload",
                "--thread-id",
                "1531235755677188116",
                "--city",
                "桃園市",
                "--district",
                "中壢區",
                "--case-number",
                "1050100025324",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert code == 0
    assert _BrokerHandler.received == {
        "thread_id": "1531235755677188116",
        "city": "桃園市",
        "district": "中壢區",
        "case_number": "1050100025324",
    }
    assert json.loads(capsys.readouterr().out)["filenames"] == ["1050100025324_1_1.pdf"]


def test_node_broker_uploads_only_matching_original_pdf(tmp_path: Path) -> None:
    case_dir = tmp_path / "桃園市" / "中壢區" / "1050100025324"
    case_dir.mkdir(parents=True)
    (case_dir / "1050100025324_1_1.pdf").write_bytes(b"%PDF-original")
    (case_dir / "案件詳細資料.pdf").write_bytes(b"%PDF-summary-must-not-upload")

    discord = ThreadingHTTPServer(("127.0.0.1", 0), _DiscordHandler)
    discord_thread = threading.Thread(target=discord.serve_forever, daemon=True)
    discord_thread.start()
    broker_port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "OPENAB_DISCORD_BOT_TOKEN": "offline-test-token",
            "RADAR_AUCTION_DOWNLOAD_DIR": str(tmp_path),
            "RADAR_PDF_ALLOWED_PARENT_IDS": "1530076529751756870",
            "RADAR_PDF_BROKER_PORT": str(broker_port),
            "RADAR_DISCORD_API_BASE_URL": (
                f"http://127.0.0.1:{discord.server_address[1]}/api/v10"
            ),
        }
    )
    broker = subprocess.Popen(
        ["node", str(REPO_ROOT / "openab" / "gateway" / "pdf-upload-broker.mjs")],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{broker_port}/health", timeout=0.5
                ):
                    break
            except Exception:
                if broker.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail(f"broker failed to start: {broker.stderr.read()}")
                time.sleep(0.05)

        payload = json.dumps(
            {
                "thread_id": "1531235755677188116",
                "city": "桃園市",
                "district": "中壢區",
                "case_number": "1050100025324",
            },
            ensure_ascii=False,
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{broker_port}/upload-auction-pdf",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode())
        assert result["filenames"] == ["1050100025324_1_1.pdf"]
        assert result["message_url"] == (
            "https://discord.com/channels/1530072733818556538/"
            "1531235755677188116/1531240484385591447"
        )
        assert result["attachments"] == [
            {
                "filename": "1050100025324_1_1.pdf",
                "url": "https://cdn.discord.test/1050100025324_1_1.pdf",
            }
        ]
        assert b"1050100025324_1_1.pdf" in _DiscordHandler.upload_body
        assert "案件詳細資料.pdf".encode() not in _DiscordHandler.upload_body
    finally:
        broker.terminate()
        broker.wait(timeout=5)
        discord.shutdown()
        discord.server_close()
        discord_thread.join(timeout=2)
