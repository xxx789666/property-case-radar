"""Coverage for the minimal ACP stdio<->TCP bridge
(openab/gateway/bridge-client.mjs + openab/sidecar/bridge-server.mjs) that
lets OpenAB (gateway container) talk to codex-acp (sidecar container)
across the two-container split -- OpenAB itself has no built-in remote/TCP
agent transport (confirmed against the upstream README and, for wire
framing, directly against the ACP TypeScript SDK's line-buffer.js that
codex-acp is built on: ACP-over-stdio is newline-delimited JSON).

Runs the real Node.js scripts as subprocesses over a real loopback TCP
connection -- no live Discord, no real Codex/database credentials, no
Docker required for this file (the full two-container Docker roundtrip is
covered separately in tests/test_openab_two_container_split.py).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_CLIENT = REPO_ROOT / "openab" / "gateway" / "bridge-client.mjs"
BRIDGE_SERVER = REPO_ROOT / "openab" / "sidecar" / "bridge-server.mjs"
FAKE_AGENT = REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed in this environment")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bridge_scripts_have_valid_node_syntax() -> None:
    for script in (BRIDGE_CLIENT, BRIDGE_SERVER, FAKE_AGENT):
        result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_bridge_client_never_shells_out_or_execs_arbitrary_commands() -> None:
    """The gateway's bridge client must be a pure byte relay -- no shell,
    no child_process, no fs write capability beyond its own stdio/socket.
    """
    source = BRIDGE_CLIENT.read_text(encoding="utf-8")
    for forbidden in ("child_process", "exec(", "execSync", "spawn(", "eval(", "require(\"fs\")", "require('fs')"):
        assert forbidden not in source, f"bridge-client.mjs must never reference {forbidden!r}"


class _BridgeServerProcess:
    def __init__(self, port: int, agent_args: str, db_secret_path: Path) -> None:
        import os

        env = os.environ.copy()
        env["ACP_SIDECAR_LISTEN_PORT"] = str(port)
        env["ACP_SIDECAR_LISTEN_HOST"] = "127.0.0.1"
        env["ACP_AGENT_COMMAND"] = "node"
        env["ACP_AGENT_ARGS"] = agent_args
        env["ACP_AGENT_CWD"] = str(REPO_ROOT)  # /workspace/radar-agent doesn't exist on the test host
        env["RADAR_AGENT_DATABASE_URL_SECRET_FILE"] = str(db_secret_path)
        self.port = port
        self.proc = subprocess.Popen(
            ["node", str(BRIDGE_SERVER)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    def stop(self) -> tuple[str, str]:
        self.proc.terminate()
        try:
            return self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.communicate(timeout=10)


@pytest.fixture
def bridge_server(tmp_path):
    db_secret_path = tmp_path / "fake_db_secret"
    db_secret_path.write_text("sqlite+pysqlite:////tmp/fake-not-a-real-db.db", encoding="utf-8")
    port = _free_tcp_port()
    server = _BridgeServerProcess(port, str(FAKE_AGENT), db_secret_path)
    time.sleep(0.5)  # bounded startup wait, not a busy-poll
    yield server
    server.stop()


def test_server_fails_closed_without_the_database_secret_file(tmp_path) -> None:
    import os

    missing_path = tmp_path / "does-not-exist"
    env = os.environ.copy()
    env["ACP_SIDECAR_LISTEN_PORT"] = str(_free_tcp_port())
    env["RADAR_AGENT_DATABASE_URL_SECRET_FILE"] = str(missing_path)
    result = subprocess.run(["node", str(BRIDGE_SERVER)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert str(missing_path) in result.stderr or "cannot read" in result.stderr


def _run_client(port: int, *, writes: list[bytes], inter_write_delay: float = 0.3, timeout: float = 15) -> tuple[bytes, str, int]:
    import os

    env = os.environ.copy()
    env["ACP_SIDECAR_HOST"] = "127.0.0.1"
    env["ACP_SIDECAR_PORT"] = str(port)
    client = subprocess.Popen(
        ["node", str(BRIDGE_CLIENT)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    for chunk in writes:
        client.stdin.write(chunk)
        client.stdin.flush()
        time.sleep(inter_write_delay)
    client.stdin.close()
    out, err = client.communicate(timeout=timeout)
    return out, err.decode("utf-8", errors="replace"), client.returncode


def test_roundtrip_relays_request_and_response_through_both_hops(bridge_server) -> None:
    out, err, code = _run_client(bridge_server.port, writes=[b"hello\n", b"world\n"])
    assert out == b"echo:hello\necho:world\n", err
    assert code == 0


def test_framing_preserved_when_a_line_is_written_in_fragments(bridge_server) -> None:
    """A single logical ACP message split across multiple small writes (as
    a real TCP stream might deliver it) must still arrive as one coherent
    line on the other side -- proving the relay never re-frames or
    corrupts message boundaries, since it never parses them at all.
    """
    out, err, code = _run_client(
        bridge_server.port,
        writes=[b"frag", b"mented", b"-message", b"\n"],
        inter_write_delay=0.05,
    )
    assert out == b"echo:fragmented-message\n", err
    assert code == 0


def test_multiple_messages_in_one_write_are_each_answered(bridge_server) -> None:
    out, err, code = _run_client(bridge_server.port, writes=[b"one\ntwo\nthree\n"], inter_write_delay=0.3)
    assert out == b"echo:one\necho:two\necho:three\n", err
    assert code == 0


def test_large_payload_survives_the_relay_intact(bridge_server) -> None:
    """A large single line (bigger than typical socket/pipe buffer sizes)
    must arrive byte-for-byte intact -- the practical proxy for
    backpressure working correctly: pipe()'s flow control must pause/
    resume as needed rather than silently truncating or corrupting data
    under load.
    """
    big_line = ("x" * 2_000_000).encode("ascii") + b"\n"
    out, err, code = _run_client(bridge_server.port, writes=[big_line], inter_write_delay=0.5, timeout=30)
    assert out == b"echo:" + big_line, f"payload corrupted or truncated; stderr={err}"
    assert code == 0


def test_bounded_disconnect_when_sidecar_is_unreachable() -> None:
    """No bridge-server running at all (connection refused) -- the client
    must fail closed within a bound, never hang waiting for a connection
    that will never succeed.
    """
    port = _free_tcp_port()  # nothing listening here
    start = time.monotonic()
    out, err, code = _run_client(port, writes=[b"hello\n"], inter_write_delay=0.05, timeout=10)
    elapsed = time.monotonic() - start
    assert code != 0
    assert elapsed < 8, f"client took {elapsed:.1f}s to fail on an unreachable sidecar; must be bounded"


def test_bounded_disconnect_when_agent_process_crashes(bridge_server) -> None:
    """The sidecar's spawned agent exiting unexpectedly mid-session
    (simulated cross-platform via the fake agent's "CRASH_NOW" trigger,
    rather than an OS-specific "find and kill a child process" mechanism)
    -- the server must notice within its shutdown bound and abortively
    close the connection (TCP RST via resetAndDestroy()), and the client
    must fail closed (nonzero exit, via its existing socket "error"
    handler) rather than exiting 0 as if the session had ended normally,
    and rather than hanging forever waiting for a response that will
    never arrive.
    """
    import os

    env = os.environ.copy()
    env["ACP_SIDECAR_HOST"] = "127.0.0.1"
    env["ACP_SIDECAR_PORT"] = str(bridge_server.port)
    client = subprocess.Popen(
        ["node", str(BRIDGE_CLIENT)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    client.stdin.write(b"CRASH_NOW\n")
    client.stdin.flush()

    start = time.monotonic()
    try:
        out, err = client.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        client.kill()
        pytest.fail("client did not exit within a bounded time after the agent process crashed")
    elapsed = time.monotonic() - start
    assert client.returncode != 0, "an agent crash must fail closed (nonzero exit), never look like a clean session end"
    assert elapsed < 12, f"disconnect took {elapsed:.1f}s; must be bounded by ACP_BRIDGE_SHUTDOWN_TIMEOUT_MS"


def test_client_fails_closed_without_required_env_vars() -> None:
    import os

    env = os.environ.copy()
    env.pop("ACP_SIDECAR_HOST", None)
    env.pop("ACP_SIDECAR_PORT", None)
    result = subprocess.run(
        ["node", str(BRIDGE_CLIENT)], env=env, input=b"", capture_output=True, timeout=10
    )
    assert result.returncode != 0
    assert b"ACP_SIDECAR_HOST" in result.stderr
