"""Offline config/security checks for the OpenAB / Codex ACP Discord bridge's
two-container split (openab/gateway/, openab/sidecar/). No live Discord, no
real token, no network required for any of these -- everything here is
static/local. Docker-dependent checks skip cleanly when the `docker` CLI
isn't available rather than failing the run.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAB_DIR = REPO_ROOT / "openab"
GATEWAY_DIR = OPENAB_DIR / "gateway"
SIDECAR_DIR = OPENAB_DIR / "sidecar"
CONFIG_TEMPLATE_PATH = GATEWAY_DIR / "config-radar-agent.toml"

EXPECTED_ALLOWED_CHANNELS = {"1530076451242508318", "1530076529751756870"}
EXPECTED_ALLOWED_USERS = [
    "843428445802725388",
    "1149306408085508106",
]


def _load_runtime_config_module():
    spec = importlib.util.spec_from_file_location(
        "radar_agent_runtime_config", GATEWAY_DIR / "runtime_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rc():
    return _load_runtime_config_module()


@pytest.fixture(scope="module")
def template_text() -> str:
    return CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")


def _filled_template(template_text: str) -> str:
    return template_text


# --- template structure -----------------------------------------------


def test_committed_template_is_valid_toml_with_no_leaked_secret(template_text: str) -> None:
    parsed = tomllib.loads(template_text)
    discord_cfg = parsed["discord"]
    assert discord_cfg["bot_token"] == "${OPENAB_DISCORD_BOT_TOKEN}"
    assert set(discord_cfg["allowed_channels"]) == EXPECTED_ALLOWED_CHANNELS
    assert discord_cfg["allow_dm"] is False
    assert discord_cfg["allowed_users"] == EXPECTED_ALLOWED_USERS
    assert parsed["pool"]["max_sessions"] == 1


def test_committed_template_never_contains_a_plausible_real_token(template_text: str) -> None:
    # A real Discord bot token is a long base64url-ish string with two
    # dots. The only such-shaped strings in this file must be the literal
    # ${...} placeholder, never something that looks like real secret
    # material accidentally left in during editing.
    assert "${OPENAB_DISCORD_BOT_TOKEN}" in template_text
    for line in template_text.splitlines():
        if "bot_token" in line:
            assert line.strip() == 'bot_token = "${OPENAB_DISCORD_BOT_TOKEN}"'


def test_committed_template_never_hardcodes_a_database_credential(template_text: str) -> None:
    # The gateway config's actual TOML values (not its prose comments,
    # which necessarily discuss the sidecar's DB secret to explain why
    # this file never references it) must never contain the DB secret.
    parsed = tomllib.loads(template_text)
    flattened = str(parsed)
    assert "DATABASE_URL" not in flattened
    assert "radar_agent_database_url" not in flattened
    assert "${ACP_SIDECAR_HOST}" in flattened
    assert "${ACP_SIDECAR_PORT}" in flattened


def test_committed_template_agent_command_is_the_bridge_client_not_codex_acp(template_text: str) -> None:
    parsed = tomllib.loads(template_text)
    agent_cfg = parsed["agent"]
    assert agent_cfg["command"] == "/opt/radar-agent/bridge-client.mjs"
    assert "codex-acp" not in agent_cfg["command"]


# --- runtime_config.py fail-closed behavior -----------------------------


def test_runtime_config_rejects_missing_token(rc, template_text: str) -> None:
    with pytest.raises(rc.ConfigError, match="missing or contains invalid whitespace"):
        rc.render_config_text(template_text, token=None)


def test_runtime_config_rejects_token_with_whitespace(rc, template_text: str) -> None:
    with pytest.raises(rc.ConfigError, match="invalid whitespace"):
        rc.render_config_text(template_text, token="has a space")


def test_runtime_config_rejects_empty_allowed_users(rc, template_text: str) -> None:
    empty = template_text.replace('  "843428445802725388",\n', "").replace(
        '  "1149306408085508106",\n',
        "",
    )
    with pytest.raises(rc.ConfigError, match="allowed_users must be a non-empty allowlist"):
        rc.render_config_text(empty, token="fake-token-for-offline-render-test")


def test_runtime_config_accepts_a_correctly_filled_template(rc, template_text: str) -> None:
    filled = _filled_template(template_text)
    rendered = rc.render_config_text(filled, token="fake-token-for-offline-render-test")
    assert "fake-token-for-offline-render-test" in rendered
    assert "${OPENAB_DISCORD_BOT_TOKEN}" not in rendered
    # Left for OpenAB's own native ${VAR} expansion -- non-secret sidecar
    # address/port on the internal Compose network, not a runtime_config.py
    # placeholder (there is nothing secret here to protect).
    assert "${ACP_SIDECAR_HOST}" in rendered
    assert "${ACP_SIDECAR_PORT}" in rendered


def test_runtime_config_overrides_static_users_from_runtime_allowlist(rc, template_text: str) -> None:
    runtime_users = ["1488170559865884684", "843428445802725388"]
    rendered = rc.render_config_text(
        template_text,
        token="fake-token-for-offline-render-test",
        allowed_users=runtime_users,
    )
    parsed = tomllib.loads(rendered)
    assert parsed["discord"]["allowed_users"] == sorted(runtime_users)
    assert "1149306408085508106" not in parsed["discord"]["allowed_users"]


def test_runtime_config_rejects_empty_runtime_allowlist(rc, template_text: str) -> None:
    with pytest.raises(rc.ConfigError, match="runtime allowed-users file"):
        rc.render_config_text(
            template_text,
            token="fake-token-for-offline-render-test",
            allowed_users=[],
        )


def test_runtime_config_rejects_wrong_channel_set(rc, template_text: str) -> None:
    filled = _filled_template(template_text)
    bad = filled.replace("1530076529751756870", "9999999999999999999")
    with pytest.raises(rc.ConfigError, match="allowed_channels must be exactly"):
        rc.render_config_text(bad, token="fake-token-for-offline-render-test")


def test_runtime_config_rejects_allow_dm_true(rc, template_text: str) -> None:
    filled = _filled_template(template_text)
    bad = filled.replace("allow_dm = false", "allow_dm = true")
    with pytest.raises(rc.ConfigError, match="allow_dm must be explicitly false"):
        rc.render_config_text(bad, token="fake-token-for-offline-render-test")


def test_runtime_config_rejects_allow_all_users(rc, template_text: str) -> None:
    bad = template_text.replace("[discord]\n", "[discord]\nallow_all_users = true\n", 1)
    with pytest.raises(rc.ConfigError, match="allow_all_users must not be true"):
        rc.render_config_text(bad, token="fake-token-for-offline-render-test")


def test_runtime_config_rejects_non_numeric_allowed_user(rc, template_text: str) -> None:
    bad = template_text.replace("843428445802725388", "not-a-snowflake")
    with pytest.raises(rc.ConfigError, match="numeric Discord user ID"):
        rc.render_config_text(bad, token="fake-token-for-offline-render-test")


def test_runtime_config_writes_a_0600_file_atomically(rc, template_text: str, tmp_path) -> None:
    filled = _filled_template(template_text)
    template_path = tmp_path / "config.toml"
    template_path.write_text(filled, encoding="utf-8")
    output_path = tmp_path / "runtime" / "openab.toml"

    os.environ["OPENAB_DISCORD_BOT_TOKEN"] = "fake-token-for-offline-render-test"
    try:
        exit_code = rc.main([str(template_path), str(output_path)])
    finally:
        del os.environ["OPENAB_DISCORD_BOT_TOKEN"]

    assert exit_code == 0
    assert "fake-token-for-offline-render-test" in output_path.read_text(encoding="utf-8")
    if sys.platform != "win32":
        # os.chmod is a limited emulation on Windows -- the real
        # permission guarantee is verified against a genuine Linux
        # filesystem in openab/README.md's documented offline checks.
        assert oct(output_path.stat().st_mode & 0o777) == "0o600"


# --- gateway shell entrypoint: syntax + fail-closed behavior ------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_gateway_entrypoint_has_valid_bash_syntax() -> None:
    # Piped via stdin as raw bytes (not text=True) rather than passed as a
    # file-path argument: on Windows Git Bash, a plain subprocess spawn of
    # bash.exe doesn't perform the MSYS /c/... path translation an
    # interactive Git Bash shell would, so a path argument (in either
    # Windows or MSYS form) spuriously "not found"s here even though the
    # file exists -- and text=True's newline translation on Windows turns
    # this script's `\`-newline line continuations into `\`-CRLF, which
    # bash parses as a syntax error even though the file itself is fine.
    source = (GATEWAY_DIR / "container-entrypoint.sh").read_text(encoding="utf-8")
    result = subprocess.run(["bash", "-n"], input=source.encode("utf-8"), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


# The POSIX `sh -n` syntax check for container-entrypoint.sh lives in
# tests/test_openab_two_container_split.py instead of here: that file
# already builds the real gateway image, and running `sh -n` INSIDE it
# (the image's own bundled /bin/sh) verifies syntax without depending on
# whatever `sh` binary (or lack of one) happens to be on the host running
# the test suite -- a real requirement on Windows, where there is no
# native POSIX sh at all outside of Git Bash/MSYS/WSL.


def test_gateway_entrypoint_reads_token_from_a_secret_file_never_an_env_var() -> None:
    source = (GATEWAY_DIR / "container-entrypoint.sh").read_text(encoding="utf-8")
    assert "/run/secrets/openab_discord_bot_token" in source
    # Command-scoped assignment (`VAR=value command`), not `export` --
    # this shell's own environment (and therefore PID 1's
    # /proc/1/environ) must never hold the token at any point. See
    # openab/README.md's "security review history" round 2 for why
    # `export`-then-`unset` doesn't achieve this (verified live).
    instruction_lines = "\n".join(line for line in source.splitlines() if line.strip() and not line.strip().startswith("#"))
    assert "export OPENAB_DISCORD_BOT_TOKEN" not in instruction_lines
    assert 'OPENAB_DISCORD_BOT_TOKEN="$(cat' in source


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_gateway_entrypoint_actually_exits_nonzero_without_token_secret(tmp_path: Path) -> None:
    """Not just a source-text check -- runs the real script (piped via
    stdin, same rationale as the syntax-check test above) and confirms the
    fail-closed behavior actually happens when
    /run/secrets/openab_discord_bot_token simply doesn't exist (true here,
    since this isn't running inside the real container). The script's own
    ``/run/radar-agent`` runtime dir is redirected to a writable tmp_path
    first -- inside the real container that path is a tmpfs mount this
    container's own uid owns; outside it (this dev host, not running as
    that container at all) it is not writable, which would otherwise fail
    the script before it ever reaches the token check this test targets.
    """
    source = (GATEWAY_DIR / "container-entrypoint.sh").read_text(encoding="utf-8")
    fake_runtime_dir = tmp_path / "radar-agent-runtime"
    patched = source.replace('runtime_dir=/run/radar-agent', f'runtime_dir={fake_runtime_dir.as_posix()}')
    assert patched != source, "container-entrypoint.sh's runtime_dir assignment line must be patchable for this test"
    env = os.environ.copy()
    result = subprocess.run(["bash", "-s", "--"], input=patched.encode("utf-8"), capture_output=True, timeout=10, env=env)
    assert result.returncode != 0
    assert b"openab_discord_bot_token" in result.stderr


def test_gateway_entrypoint_deletes_rendered_config_after_starting_openab() -> None:
    source = (GATEWAY_DIR / "container-entrypoint.sh").read_text(encoding="utf-8")
    start_index = source.index("openab run -c")
    delete_index = source.index('rm -f "$runtime_config"')
    assert start_index < delete_index, "openab must be started before its rendered config is deleted"
    # No root anywhere -- see docstring comment at the top of the script
    # and openab/README.md's "least privilege" section.
    assert "chown root" not in source
    assert "USER root" not in source


def test_gateway_entrypoint_never_references_a_database_credential() -> None:
    # Only actual instruction lines matter -- the script's own top comment
    # block necessarily discusses codex-acp/the DB secret in prose to
    # explain why this container never touches them.
    source = (GATEWAY_DIR / "container-entrypoint.sh").read_text(encoding="utf-8")
    instruction_lines = "\n".join(line for line in source.splitlines() if line.strip() and not line.strip().startswith("#"))
    for forbidden in ("radar_agent_database_url", "DATABASE_URL", "codex-acp", "docker exec"):
        assert forbidden not in instruction_lines, f"gateway container-entrypoint.sh must never reference {forbidden!r}"


# --- docker-compose wiring: two services, four networks -----------------


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_compose_openab_profile_validates_offline() -> None:
    result = subprocess.run(
        ["docker", "compose", "--profile", "openab", "config", "--no-env-resolution", "--quiet"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_compose_default_profile_is_unaffected_by_the_new_services() -> None:
    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["postgres"]


def test_compose_services_are_profile_gated_and_use_file_based_secrets_not_env_file() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose_text.count('profiles: ["openab"]') == 2
    assert "openab-gateway:" in compose_text
    assert "openab-sidecar:" in compose_text
    # File-based Docker secrets, not env_file -- see openab/README.md's
    # "security review history" round 2 for why (a container's own
    # environment/env_file values become part of PID 1's immutable
    # /proc/1/environ; a secret file does not).
    assert "openab/.local/openab_discord_bot_token" in compose_text
    assert "openab/.local/radar_agent_database_url" in compose_text
    instruction_lines = "\n".join(
        line for line in compose_text.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    assert "env_file" not in instruction_lines
    # The main app's .env must never be handed to either service.
    assert "- .env\n" not in compose_text
    assert "- ./.env" not in compose_text


def test_compose_top_level_secrets_block_sources_both_gitignored_files() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    secrets_index = compose_text.rindex("secrets:")  # the top-level block, not either per-service list
    top_level_secrets = compose_text[secrets_index:]
    assert "openab_discord_bot_token" in top_level_secrets
    assert "radar_agent_database_url" in top_level_secrets
    assert "file: ./openab/.local/openab_discord_bot_token" in top_level_secrets
    assert "file: ./openab/.local/radar_agent_database_url" in top_level_secrets


def _service_block(compose_text: str, service_name: str) -> str:
    start = compose_text.index(f"\n  {service_name}:\n")
    # Next top-level (2-space-indented) key after this service starts the
    # next block -- every line of this service's own body is indented >=4.
    rest = compose_text[start + 1 :]
    lines = rest.splitlines()
    body = [lines[0]]
    for line in lines[1:]:
        if line and not line.startswith("    ") and not line.startswith("  #"):
            break
        body.append(line)
    return "\n".join(body)


def _instruction_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def test_gateway_service_has_only_the_discord_secret_and_bridge_network() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service_text = _instruction_lines(_service_block(compose_text, "openab-gateway"))
    assert "openab_discord_bot_token" in service_text
    assert "radar_agent_database_url" not in service_text
    assert "radar-internal-bridge" in service_text
    assert "radar-gateway-egress" in service_text
    # The two properties that matter most: no path to the DB network, no
    # Docker socket, no host PID namespace.
    assert "radar-db-net" not in service_text
    assert "/var/run/docker.sock" not in service_text
    assert "pid: host" not in service_text
    assert "network_mode" not in service_text


def test_sidecar_service_has_only_the_db_secret_and_expected_networks() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service_text = _instruction_lines(_service_block(compose_text, "openab-sidecar"))
    assert "radar_agent_database_url" in service_text
    assert "openab_discord_bot_token" not in service_text
    assert "radar-internal-bridge" in service_text
    assert "radar-db-net" in service_text
    assert "radar-sidecar-egress" in service_text
    assert "radar-gateway-egress" not in service_text
    assert "/var/run/docker.sock" not in service_text
    assert "pid: host" not in service_text
    assert "network_mode" not in service_text


# Gateway and sidecar deliberately run as different, non-shared UIDs --
# see Dockerfile.sidecar's own comment on why the two trust domains must
# not additionally share a UID on top of their disjoint credentials.
_SERVICE_UID = {"openab-gateway": "1000", "openab-sidecar": "1001"}


def test_compose_service_secrets_use_non_root_uid_gid_and_narrow_mode() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service_name, uid in _SERVICE_UID.items():
        service_text = _service_block(compose_text, service_name)
        secrets_start = service_text.index("    secrets:")
        secrets_section = service_text[secrets_start:]
        assert f'uid: "{uid}"' in secrets_section, service_name
        assert f'gid: "{uid}"' in secrets_section, service_name
        assert "mode: 0400" in secrets_section, service_name


def test_compose_services_use_distinct_non_root_uids() -> None:
    """Gateway and sidecar must not share a UID -- a same-UID setup would
    be one less property standing between a filesystem/permission mixup
    and cross-container access to the other's credential material.
    """
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    uids = set()
    for service_name, uid in _SERVICE_UID.items():
        service_text = _service_block(compose_text, service_name)
        assert f'user: "{uid}:{uid}"' in service_text, service_name
        uids.add(uid)
    assert len(uids) == len(_SERVICE_UID), "gateway and sidecar must not share a UID"


def test_compose_services_are_hardened_non_root_with_no_new_privileges() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service_name in _SERVICE_UID:
        service_text = _service_block(compose_text, service_name)
        assert "read_only: true" in service_text, service_name
        assert 'cap_drop: ["ALL"]' in service_text, service_name
        assert 'security_opt: ["no-new-privileges:true"]' in service_text, service_name
        # Every tmpfs mount must be owned by the non-root user this
        # container runs as -- an unqualified tmpfs mount defaults to
        # root:root, which this container (no root at any point) could not
        # use at all.
        tmpfs_lines = [line for line in service_text.splitlines() if line.strip().startswith("- /")]
        assert len(tmpfs_lines) >= 1, service_name
        uid = _SERVICE_UID[service_name]
        for line in tmpfs_lines:
            assert f"uid={uid},gid={uid}" in line, f"{service_name} tmpfs mount missing uid/gid: {line}"


def test_networks_isolate_gateway_from_postgres_and_are_internal_where_required() -> None:
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    networks_index = compose_text.rindex("\nnetworks:\n")
    networks_section = compose_text[networks_index:]
    assert "radar-internal-bridge" in networks_section
    assert "radar-db-net" in networks_section
    assert "radar-gateway-egress" in networks_section
    assert "radar-sidecar-egress" in networks_section
    # internal: true must apply to the two networks that must never route
    # anywhere outside themselves.
    bridge_block = networks_section[networks_section.index("radar-internal-bridge:") :]
    assert "internal: true" in bridge_block.splitlines()[1]
    db_block = networks_section[networks_section.index("radar-db-net:") :]
    assert "internal: true" in db_block.splitlines()[1]

    postgres_block = _service_block(compose_text, "postgres")
    assert "radar-db-net" in postgres_block
    assert "radar-internal-bridge" not in postgres_block
    assert "radar-gateway-egress" not in postgres_block
    assert "radar-sidecar-egress" not in postgres_block


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_compose_hardened_flags_and_network_topology_actually_resolve() -> None:
    """docker compose config --no-env-resolution only validates syntax --
    this confirms Compose actually resolves the hardening block (user/
    read_only/cap_drop/security_opt/tmpfs) and the network topology into
    the rendered service spec, not merely that the YAML parses.
    """
    result = subprocess.run(
        ["docker", "compose", "--profile", "openab", "config"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    assert rendered.count("no-new-privileges:true") == 2
    # >=2 rather than ==2: the gateway's read-only config bind mount also
    # renders its own "read_only: true" line, in addition to each
    # service's own top-level read_only:true.
    assert rendered.count("read_only: true") >= 2
    assert "radar-internal-bridge" in rendered
    assert "radar-db-net" in rendered


# --- Dockerfiles: non-root, reduced code surface, frozen apt snapshot ----


@pytest.mark.parametrize("dockerfile_path", [GATEWAY_DIR / "Dockerfile.gateway", SIDECAR_DIR / "Dockerfile.sidecar"])
def test_dockerfile_final_user_is_non_root(dockerfile_path: Path) -> None:
    # Gateway keeps the base image's own "node" user (uid 1000); sidecar
    # uses a dedicated, distinct "sidecar" user (uid 1001) -- see
    # Dockerfile.sidecar's own comment on why the two must not share a UID.
    expected = "USER sidecar" if dockerfile_path.name == "Dockerfile.sidecar" else "USER node"
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    user_lines = [line for line in dockerfile_text.splitlines() if line.strip().startswith("USER ")]
    assert user_lines, f"{dockerfile_path} must have at least one USER directive"
    assert user_lines[-1].strip() == expected, f"final USER must be non-root, got {user_lines[-1]!r}"


@pytest.mark.parametrize("dockerfile_path", [GATEWAY_DIR / "Dockerfile.gateway", SIDECAR_DIR / "Dockerfile.sidecar"])
def test_dockerfile_pins_a_frozen_debian_snapshot_for_apt(dockerfile_path: Path) -> None:
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    assert "snapshot.debian.org/archive/debian/" in dockerfile_text
    assert "sed -i" in dockerfile_text
    assert "Check-Valid-Until" in dockerfile_text
    assert "python3=3.13.5-1" in dockerfile_text


@pytest.mark.parametrize("dockerfile_path", [GATEWAY_DIR / "Dockerfile.gateway", SIDECAR_DIR / "Dockerfile.sidecar"])
def test_dockerfile_removes_the_live_github_cli_apt_source(dockerfile_path: Path) -> None:
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    assert "rm -f /etc/apt/sources.list.d/github-cli.list" in dockerfile_text


def test_gateway_dockerfile_never_copies_any_radar_application_code() -> None:
    dockerfile_text = (GATEWAY_DIR / "Dockerfile.gateway").read_text(encoding="utf-8")
    instruction_lines = "\n".join(
        line for line in dockerfile_text.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    for forbidden in ("COPY --chown=node:node database", "COPY --chown=node:node apps", "COPY --chown=node:node tools"):
        assert forbidden not in instruction_lines, f"gateway Dockerfile must not copy {forbidden!r}"
    assert "requirements.lock" not in instruction_lines
    assert "codex-acp" not in instruction_lines


def test_gateway_dockerfile_only_copies_bridge_client_and_runtime_config() -> None:
    dockerfile_text = (GATEWAY_DIR / "Dockerfile.gateway").read_text(encoding="utf-8")
    assert "COPY --chown=node:node openab/gateway/bridge-client.mjs" in dockerfile_text
    assert "COPY --chown=node:node openab/gateway/runtime_config.py" in dockerfile_text
    assert "COPY --chown=node:node openab/gateway/container-entrypoint.sh" in dockerfile_text


def test_sidecar_dockerfile_never_copies_write_capable_or_crawler_modules() -> None:
    # Only actual instruction lines matter here -- the Dockerfile's own
    # comments explain, in prose, exactly why apps/crawlers/repositories/
    # auction_notification.py are excluded, which necessarily names them.
    dockerfile_text = (SIDECAR_DIR / "Dockerfile.sidecar").read_text(encoding="utf-8")
    instruction_lines = "\n".join(
        line for line in dockerfile_text.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    for forbidden in ("COPY --chown=node:node apps", "database/repositories", "crawlers/", "auction_notification.py"):
        assert forbidden not in instruction_lines, f"sidecar Dockerfile must not copy {forbidden!r} into the image"


def test_sidecar_dockerfile_never_copies_discord_token_material() -> None:
    dockerfile_text = (SIDECAR_DIR / "Dockerfile.sidecar").read_text(encoding="utf-8")
    assert "openab_discord_bot_token" not in dockerfile_text
    assert "bridge-client.mjs" not in dockerfile_text
    assert "runtime_config.py" not in dockerfile_text


def test_sidecar_dockerfile_pre_creates_codex_auth_dir_owned_by_non_root() -> None:
    dockerfile_text = (SIDECAR_DIR / "Dockerfile.sidecar").read_text(encoding="utf-8")
    assert "-o sidecar -g sidecar" in dockerfile_text
    assert "/home/sidecar/.codex" in dockerfile_text


def test_sidecar_dockerfile_creates_a_dedicated_user_distinct_from_gateway() -> None:
    dockerfile_text = (SIDECAR_DIR / "Dockerfile.sidecar").read_text(encoding="utf-8")
    assert "useradd --uid 1001" in dockerfile_text
    assert "groupadd --gid 1001" in dockerfile_text


def test_sidecar_dockerfile_installs_hash_locked_requirements() -> None:
    dockerfile_text = (SIDECAR_DIR / "Dockerfile.sidecar").read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile_text
    assert "--require-hashes" in dockerfile_text


# --- .dockerignore --------------------------------------------------------


def test_dockerignore_excludes_git_env_and_secrets() -> None:
    dockerignore_path = REPO_ROOT / ".dockerignore"
    assert dockerignore_path.exists(), "repo root must have a .dockerignore for openab's build context"
    text = dockerignore_path.read_text(encoding="utf-8")
    for required in (".git/", ".env", "openab/.local/", "__pycache__", "*.py[cod]", ".venv/", "*.db"):
        assert required in text, f".dockerignore must exclude {required!r}"


# --- secrets hygiene ------------------------------------------------------


def test_gitignore_covers_the_openab_secrets_directory() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "openab/.local/" in gitignore


def test_secret_example_templates_are_placeholders_not_real_values() -> None:
    token_example = (OPENAB_DIR / "openab_discord_bot_token.example").read_text(encoding="utf-8").strip()
    db_example = (OPENAB_DIR / "radar_agent_database_url.example").read_text(encoding="utf-8").strip()
    assert "REPLACE" in token_example
    assert "REPLACE_ME" in db_example
    assert db_example.startswith("postgresql+psycopg://")
    # Not a base64url-ish two-dot string shaped like a real Discord token.
    assert token_example.count(".") == 0


def test_no_local_secrets_file_is_accidentally_tracked() -> None:
    # git check-ignore works on a path whether or not it currently exists,
    # so this equally covers "bootstrap already ran" and "not yet
    # bootstrapped" -- either way, these two paths must be gitignored.
    local_dir = OPENAB_DIR / ".local"
    for name in ("openab_discord_bot_token", "radar_agent_database_url"):
        result = subprocess.run(["git", "check-ignore", "-q", str(local_dir / name)], cwd=REPO_ROOT, timeout=10)
        assert result.returncode == 0, f"openab/.local/{name} must be gitignored"


# --- bootstrap SQL: safe password flow + exact privilege shape ----------


def test_bootstrap_read_only_role_sql_never_hardcodes_a_password() -> None:
    sql = (SIDECAR_DIR / "bootstrap_read_only_role.sql").read_text(encoding="utf-8")
    assert "CHANGE_ME" not in sql
    # Takes the password as a psql variable, substituted via the
    # quoted-literal form (:'ro_password'), never string-concatenated --
    # and refuses to run at all if the variable wasn't supplied.
    assert ":'ro_password'" in sql
    assert "\\if :{?ro_password}" in sql
    # NOT bare \quit: psql's \q/\quit has no exit-code argument (verified
    # against a real psql 16 -- it always exits 0), which would let
    # automation mistake a missing-password abort for a successful
    # bootstrap. \set ON_ERROR_STOP on + a statement guaranteed to fail is
    # what actually makes psql itself exit non-zero here -- verified live
    # in tests/test_radar_agent_query.py against a real disposable
    # Postgres (both with and without an explicit -v ON_ERROR_STOP=1 on
    # the invocation).
    assert "\\set ON_ERROR_STOP on" in sql
    assert "1/0" in sql


def test_bootstrap_read_only_role_sql_matches_the_verification_expectations() -> None:
    sql = (SIDECAR_DIR / "bootstrap_read_only_role.sql").read_text(encoding="utf-8")
    assert "CREATE ROLE radar_agent_ro" in sql
    assert "NOSUPERUSER" in sql and "NOCREATEDB" in sql and "NOCREATEROLE" in sql and "NOBYPASSRLS" in sql
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO radar_agent_ro" in sql
    assert "REVOKE TEMP ON DATABASE radar FROM PUBLIC" in sql
    assert "REVOKE TEMP ON DATABASE radar FROM radar_agent_ro" in sql
    assert "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM radar_agent_ro" in sql
    # Must never grant anything beyond SELECT/CONNECT/USAGE-on-schema.
    for forbidden in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE", "GRANT TRUNCATE", "GRANT CREATE", "GRANT ALL", "GRANT TEMP"):
        assert forbidden not in sql, f"bootstrap SQL must never {forbidden}"
    # No sequence privilege at all -- USAGE alone would already permit
    # nextval()/currval().
    assert "GRANT USAGE ON ALL SEQUENCES" not in sql


# A true offline parse of this file (no server) isn't meaningfully
# possible with psql -- it has no "check syntax only" mode independent of
# connecting. Real execution against a live, disposable PostgreSQL server
# is covered by
# tests/test_radar_agent_query.py::TestRealPostgresRolePrivileges, which
# applies this exact file with a real password and verifies the resulting
# role's grants -- a stronger guarantee than a syntax-only check.
