"""Real, offline, two-container verification of the gateway/sidecar split
(openab/gateway/Dockerfile.gateway + openab/sidecar/Dockerfile.sidecar).

Everything here runs against *actually built* images and *actually running*
containers on a real Docker network -- not source-level assertions (those
live in tests/test_openab_radar_agent_config.py). No live Discord connection
is ever made: the gateway container's real ENTRYPOINT (container-entrypoint.sh
-> openab run, which would try to reach Discord's real API) is never
invoked here -- these tests override it to run bridge-client.mjs directly,
the same pure byte-relay subprocess OpenAB would itself spawn, which never
touches Discord or any credential. The sidecar's real ENTRYPOINT
(bridge-server.mjs) IS safe to run as-is: it never contacts Discord, and its
"agent" is swapped for the offline fake_acp_agent.mjs fixture instead of
real codex-acp, so no live Codex/OpenAI network call happens either. No real
Discord token or database credential is used anywhere in this file --
constants below are fixture values, never a live secret.

Skips (the images, not individual assertions) when Docker itself isn't
available. Building the images is a one-time cost handled by a session-scoped
fixture; `docker build` reuses layer cache, so this is fast on a rebuild.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_AGENT = REPO_ROOT / "tests" / "fixtures" / "fake_acp_agent.mjs"

GATEWAY_IMAGE = "radar-gateway:pytest"
SIDECAR_IMAGE = "radar-sidecar:pytest"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed in this environment")


def _run(*args: str, timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture(scope="module")
def built_images():
    """Build both images once for this module. Uses Docker's normal layer
    cache (not --no-cache -- that full from-scratch rebuild is the
    dedicated, manually-run offline verification step documented in
    openab/README.md, not something every test invocation should pay for).
    """
    _run("build", "-f", "openab/gateway/Dockerfile.gateway", "-t", GATEWAY_IMAGE, ".", timeout=600)
    _run("build", "-f", "openab/sidecar/Dockerfile.sidecar", "-t", SIDECAR_IMAGE, ".", timeout=600)
    return {"gateway": GATEWAY_IMAGE, "sidecar": SIDECAR_IMAGE}


@pytest.fixture
def test_network():
    name = f"radar-pytest-bridge-{uuid.uuid4().hex[:8]}"
    _run("network", "create", "--internal", name)
    yield name
    _run("network", "rm", name, check=False)


@pytest.fixture
def fake_db_secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "fake_radar_agent_database_url"
    path.write_text("sqlite+pysqlite:////tmp/fake-not-a-real-db.db\n", encoding="utf-8")
    return path


@pytest.fixture
def fake_discord_token_file(tmp_path: Path) -> Path:
    path = tmp_path / "fake_openab_discord_bot_token"
    path.write_text("fake-token-not-real.not-real.not-real\n", encoding="utf-8")
    return path


class _Container:
    def __init__(self, name: str) -> None:
        self.name = name

    def exec(self, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
        return _run("exec", self.name, *args, timeout=timeout, check=False)

    def logs(self) -> str:
        return _run("logs", self.name, check=False).stdout + _run("logs", self.name, check=False).stderr

    def stop(self) -> None:
        _run("rm", "-f", self.name, check=False, timeout=30)


@pytest.fixture
def gateway_container(built_images, test_network, sidecar_container):
    """A long-lived gateway container running the real bridge-client.mjs
    subprocess (never openab/Discord) against the real sidecar_container,
    kept alive via an attached-but-unclosed stdin (-i, no data written) so
    /proc/1 probes below have something to inspect.
    """
    name = f"radar-pytest-gateway-long-{uuid.uuid4().hex[:8]}"
    _run(
        "run",
        "-d",
        "-i",
        "--name",
        name,
        "--network",
        test_network,
        "-e",
        f"ACP_SIDECAR_HOST={sidecar_container.name}",
        "-e",
        "ACP_SIDECAR_PORT=8765",
        "--entrypoint",
        "node",
        GATEWAY_IMAGE,
        "/opt/radar-agent/bridge-client.mjs",
    )
    container = _Container(name)
    time.sleep(1.0)
    yield container
    container.stop()


@pytest.fixture
def sidecar_container(built_images, test_network, fake_db_secret_file):
    """The sidecar's REAL entrypoint (bridge-server.mjs) running in a real
    container, with the fake agent substituted for codex-acp so no live
    Codex/OpenAI call happens, and a fixture DB secret file bind-mounted at
    the exact path the real Docker-secret mechanism would place it at.
    """
    name = f"radar-pytest-sidecar-{uuid.uuid4().hex[:8]}"
    _run(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        test_network,
        "-v",
        f"{fake_db_secret_file}:/run/secrets/radar_agent_database_url:ro",
        "-v",
        f"{FAKE_AGENT}:/opt/radar-agent/fake_acp_agent.mjs:ro",
        "-e",
        "ACP_AGENT_COMMAND=node",
        "-e",
        "ACP_AGENT_ARGS=/opt/radar-agent/fake_acp_agent.mjs",
        "-e",
        "ACP_AGENT_CWD=/workspace/radar-agent",
        "-e",
        "ACP_SIDECAR_LISTEN_HOST=0.0.0.0",
        SIDECAR_IMAGE,
    )
    container = _Container(name)
    time.sleep(1.5)  # bounded startup wait, not a busy-poll
    yield container
    container.stop()


def test_gateway_entrypoint_has_valid_posix_sh_syntax_inside_the_real_image(built_images) -> None:
    """Runs `sh -n` against container-entrypoint.sh using the actual
    gateway IMAGE's own bundled /bin/sh (Debian-based, so this is always
    present), never the host's -- this is the "Windows sh test" the
    security review requires to actually execute (not skip), and doing it
    this way means it neither depends on nor is defeated by whatever `sh`
    binary (if any) the host running the test suite happens to have. The
    script also has no bashisms ([[, arrays, etc.), so this is a real
    POSIX-sh syntax check, not merely re-checking it as bash.
    """
    source = (REPO_ROOT / "openab" / "gateway" / "container-entrypoint.sh").read_text(encoding="utf-8")
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "sh", GATEWAY_IMAGE, "-n"],
        input=source.encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_images_run_as_non_root_with_distinct_uids(built_images) -> None:
    # --entrypoint override bypasses each image's real ENTRYPOINT (which
    # would otherwise fail-closed without its secret, or start listening)
    # -- this test only cares what uid the container process itself runs
    # as, independent of any entrypoint logic. Gateway and sidecar must
    # use DIFFERENT uids (never share credentials' worth of "same
    # identity"), so this checks each image's own expected value rather
    # than a single shared constant.
    expected_uids = {GATEWAY_IMAGE: "1000", SIDECAR_IMAGE: "1001"}
    for image, expected_uid in expected_uids.items():
        result = _run("run", "--rm", "--entrypoint", "id", image, "-u")
        assert result.stdout.strip() == expected_uid, f"{image} must run as uid {expected_uid}, not root"
    assert len(set(expected_uids.values())) == 2, "gateway and sidecar must not share a uid"


def test_gateway_image_has_no_database_credential_or_radar_application_code(built_images) -> None:
    """Filesystem-level probe on the built image (no live process): the
    gateway image must not contain the sidecar's Radar application code or
    the DB secret target path. Both images share the same upstream base
    (which itself bundles a codex-acp binary), so the security boundary
    here is that the gateway's ENTRYPOINT never invokes codex-acp and this
    container never has a database credential to hand it -- not that the
    binary is physically absent from a shared base layer.
    """
    result = _run(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        GATEWAY_IMAGE,
        "-c",
        "test -d /opt/radar-agent/property-case-radar && echo FOUND_APP_CODE; "
        "test -f /run/secrets/radar_agent_database_url && echo FOUND_DB_SECRET; "
        "true",
    )
    combined = result.stdout + result.stderr
    assert "FOUND_APP_CODE" not in combined
    assert "FOUND_DB_SECRET" not in combined


def test_sidecar_image_has_no_discord_token_or_gateway_code_paths(built_images) -> None:
    result = _run(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        SIDECAR_IMAGE,
        "-c",
        "test -f /opt/radar-agent/bridge-client.mjs && echo FOUND_BRIDGE_CLIENT; "
        "test -f /opt/radar-agent/runtime_config.py && echo FOUND_RUNTIME_CONFIG; "
        "test -f /run/secrets/openab_discord_bot_token && echo FOUND_DISCORD_TOKEN_SECRET; "
        "command -v openab && echo FOUND_OPENAB_BINARY; "
        "true",
    )
    combined = result.stdout + result.stderr
    assert "FOUND_BRIDGE_CLIENT" not in combined
    assert "FOUND_RUNTIME_CONFIG" not in combined
    assert "FOUND_DISCORD_TOKEN_SECRET" not in combined
    # openab IS present as a binary in this base image (both containers
    # share the same upstream base) -- the security boundary is that this
    # container's ENTRYPOINT never invokes it and it never holds a Discord
    # token to invoke it *with*, not that the binary is physically absent.
    # This assertion documents that reality rather than asserting a false
    # guarantee.


def test_sidecar_container_never_has_the_discord_token_in_its_own_environment(sidecar_container) -> None:
    """Direct /proc/1/environ probe on the actual running container --
    proves the sidecar's real environment table contains no Discord
    token-shaped value, not merely that no file mounts one in.
    """
    result = sidecar_container.exec("cat", "/proc/1/environ")
    environ_text = result.stdout.replace("\x00", "\n")
    assert "DISCORD" not in environ_text.upper()
    assert "openab_discord_bot_token" not in environ_text


def test_sidecar_container_has_no_network_path_to_a_gateway_only_network(sidecar_container, test_network) -> None:
    """The sidecar container is only ever attached to the one test network
    it was started on (mirroring radar-internal-bridge/radar-db-net, never
    radar-gateway-egress in the real topology) -- confirmed via docker
    inspect against the live container, not just the Compose file text.
    """
    result = _run("inspect", "-f", "{{json .NetworkSettings.Networks}}", sidecar_container.name)
    assert test_network in result.stdout
    # Exactly one network -- this container was never given a second,
    # broader network to reach.
    import json

    networks = json.loads(result.stdout)
    assert list(networks.keys()) == [test_network]


def test_bridge_roundtrip_across_two_real_containers_on_the_internal_network(
    built_images, test_network, sidecar_container, fake_discord_token_file
) -> None:
    """The actual end-to-end proof: a SEPARATE gateway container, on the
    same real internal-only Docker network as the sidecar, running
    bridge-client.mjs (the exact subprocess OpenAB itself would spawn as
    its [agent] command -- invoked directly here, never via openab/Discord,
    so no live Discord connection happens) sends bytes that the sidecar
    relays to the fake agent and back, crossing a genuine container
    boundary and a genuine Docker network, not an in-process pipe.
    """
    # `docker run -i` is required (not the `_run` helper, which doesn't
    # attach stdin) so the payload below actually reaches the container's
    # stdin -- bridge-client.mjs relays exactly what arrives there. Uses
    # Popen (not subprocess.run(input=...)) with an explicit delay before
    # closing stdin: closing immediately after writing races the sidecar's
    # own child-process cold start (spawning the fake agent takes tens of
    # ms), which would half-close the relay before a response can arrive
    # (the same race documented for the in-process bridge tests in
    # tests/test_openab_bridge.py).
    gateway_name = f"radar-pytest-gateway-{uuid.uuid4().hex[:8]}"
    proc = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            gateway_name,
            "--network",
            test_network,
            "-e",
            f"ACP_SIDECAR_HOST={sidecar_container.name}",
            "-e",
            "ACP_SIDECAR_PORT=8765",
            "--entrypoint",
            "node",
            GATEWAY_IMAGE,
            "/opt/radar-agent/bridge-client.mjs",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(b"hello-from-real-gateway-container\n")
    proc.stdin.flush()
    time.sleep(1.0)
    proc.stdin.close()
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err.decode("utf-8", errors="replace")
    assert out == b"echo:hello-from-real-gateway-container\n", (
        f"stdout={out!r} stderr={err.decode('utf-8', errors='replace')}"
    )


def test_gateway_container_never_has_a_database_credential_in_its_own_environment_or_argv(gateway_container) -> None:
    """Direct /proc/1/environ AND /proc/1/cmdline probes on the actual
    running gateway container -- the sidecar's DATABASE_URL secret (or its
    file path) must appear in neither: bridge-client.mjs never receives it
    at all (it isn't even part of that script's env var contract -- only
    ACP_SIDECAR_HOST/PORT are), so this proves the negative directly
    against the live process rather than by code inspection alone.
    """
    environ_result = gateway_container.exec("cat", "/proc/1/environ")
    environ_text = environ_result.stdout.replace("\x00", "\n")
    assert "DATABASE_URL" not in environ_text.upper()
    assert "radar_agent_database_url" not in environ_text

    cmdline_result = gateway_container.exec("cat", "/proc/1/cmdline")
    cmdline_text = cmdline_result.stdout.replace("\x00", " ")
    assert "DATABASE_URL" not in cmdline_text.upper()
    assert "radar_agent_database_url" not in cmdline_text


def test_sidecar_container_pid1_argv_never_contains_the_database_credential(sidecar_container) -> None:
    """bridge-server.mjs (PID 1 in the sidecar container) reads the DB
    secret from a file at startup and only ever places it into each spawned
    codex-acp child's *env* -- never a CLI argument -- so PID 1's own argv
    must never contain the connection string, confirmed directly against
    /proc/1/cmdline on the live container.
    """
    result = sidecar_container.exec("cat", "/proc/1/cmdline")
    cmdline_text = result.stdout.replace("\x00", " ")
    assert "fake-not-a-real-db" not in cmdline_text
    assert "sqlite" not in cmdline_text.lower()


@pytest.fixture(scope="module")
def compose_sidecar_only():
    """Brings up ONLY openab-sidecar from the REAL docker-compose.yml (the
    actual multi-network, static-IP topology -- radar-internal-bridge,
    radar-db-net, radar-sidecar-egress -- not this test file's own
    single-network throwaway setup used elsewhere), to prove the real
    Compose-defined network isolation, not just an equivalent recreated by
    hand. Never starts openab-gateway (so nothing here ever attempts a
    live Discord connection); never uses a real Discord token or database
    credential (fixture-only placeholder values, gitignored, removed
    afterward).

    Safety: refuses to run (skips) if openab/.local's secret files already
    exist, rather than ever overwriting or deleting what could be a real
    deployment's actual secrets.
    """
    local_dir = REPO_ROOT / "openab" / ".local"
    token_path = local_dir / "openab_discord_bot_token"
    db_path = local_dir / "radar_agent_database_url"
    if token_path.exists() or db_path.exists():
        pytest.skip("openab/.local already has secret file(s) -- refusing to touch a possibly-real deployment secret")

    project = f"radar-pytest-compose-{uuid.uuid4().hex[:8]}"
    local_dir.mkdir(parents=True, exist_ok=True)
    token_path.write_text("fake-token-not-real.not-real.not-real\n", encoding="utf-8")
    db_path.write_text("sqlite+pysqlite:////tmp/fake-not-a-real-db.db\n", encoding="utf-8")
    try:
        subprocess.run(
            ["docker", "compose", "-p", project, "--profile", "openab", "up", "-d", "openab-sidecar"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        time.sleep(1.5)
        yield {
            "project": project,
            "bridge_network": f"{project}_radar-internal-bridge",
            "db_network": f"{project}_radar-db-net",
            "sidecar_ip": "172.28.238.10",
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-p", project, "--profile", "openab", "down", "-v"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=60,
        )
        token_path.unlink(missing_ok=True)
        db_path.unlink(missing_ok=True)
        try:
            local_dir.rmdir()
        except OSError:
            pass  # not empty / already gone -- nothing left of ours to clean up


def _tcp_probe_from_network(network: str, host: str, port: int) -> tuple[bool, str]:
    script = (
        "const net=require('node:net');"
        f"const s=net.createConnection({{host:{host!r},port:{port}}});"
        "s.setTimeout(3000);"
        "s.on('connect',()=>{console.log('CONNECTED');s.end();process.exit(0);});"
        "s.on('timeout',()=>{console.log('TIMEOUT');process.exit(1);});"
        "s.on('error',(e)=>{console.log('ERROR',e.code);process.exit(1);});"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", network, "--entrypoint", "node", GATEWAY_IMAGE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0, result.stdout.strip()


def test_real_compose_topology_db_network_peer_cannot_reach_acp_listener(built_images, compose_sidecar_only) -> None:
    """The actual regression this whole network-isolation fix targets: a
    review probe attached only to radar-db-net previously connected to and
    completed an ACP round-trip against the sidecar's listener (bound to
    0.0.0.0, reachable from every network the multi-homed sidecar is on).
    With the listener now bound to its static radar-internal-bridge
    address only, a peer on radar-db-net must get no route/refused, never
    a successful connection.
    """
    ok, output = _tcp_probe_from_network(
        compose_sidecar_only["db_network"], compose_sidecar_only["sidecar_ip"], 8765
    )
    assert not ok, f"a radar-db-net peer must NOT reach the ACP listener; got: {output}"


def test_real_compose_topology_internal_bridge_peer_can_reach_acp_listener(built_images, compose_sidecar_only) -> None:
    """Positive control: the same probe, attached to radar-internal-bridge
    instead (the network the gateway itself is actually on in production),
    must succeed -- proving the negative result above is real network
    isolation, not a broken listener.
    """
    ok, output = _tcp_probe_from_network(
        compose_sidecar_only["bridge_network"], compose_sidecar_only["sidecar_ip"], 8765
    )
    assert ok, f"a radar-internal-bridge peer must reach the ACP listener; got: {output}"


def test_gateway_container_cannot_reach_the_sidecars_listen_port_from_a_different_network(
    built_images, sidecar_container
) -> None:
    """Negative control for the roundtrip test above: a gateway container
    NOT attached to the sidecar's network must fail to connect at all --
    proving the roundtrip's success above comes from genuine network
    reachability, not from some other implicit path (e.g. Docker's default
    bridge letting everything talk to everything).
    """
    gateway_name = f"radar-pytest-gateway-isolated-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            gateway_name,
            "--network",
            "none",
            "-e",
            f"ACP_SIDECAR_HOST={sidecar_container.name}",
            "-e",
            "ACP_SIDECAR_PORT=8765",
            "--entrypoint",
            "node",
            GATEWAY_IMAGE,
            "/opt/radar-agent/bridge-client.mjs",
        ],
        input=b"should-never-arrive\n",
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert proc.stdout != b"echo:should-never-arrive\n"
