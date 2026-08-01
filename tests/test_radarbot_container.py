from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.discord_bot.auction_cog import AuctionCog
from apps.discord_bot.cog import HouseCog
from apps.discord_bot.container_main import _read_required_secret
from apps.discord_bot.main import EXPECTED_COMMAND_SCHEMA, validate_command_schema


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
DOCKERFILE_PATH = REPO_ROOT / "deploy" / "radarbot" / "Dockerfile"
BOOTSTRAP_SQL_PATH = REPO_ROOT / "deploy" / "radarbot" / "bootstrap_app_role.sql"


def test_exact_local_command_schema_matches_production_contract() -> None:
    counts = validate_command_schema([HouseCog.house, AuctionCog.auction])
    assert counts == {"house": 6, "auction": 7}
    assert EXPECTED_COMMAND_SCHEMA["house"] == {
        "search",
        "subscribe",
        "latest",
        "detail",
        "compare",
        "unsubscribe",
    }
    assert EXPECTED_COMMAND_SCHEMA["auction"] == {
        "search",
        "subscribe",
        "latest",
        "detail",
        "schedule",
        "risk",
        "unsubscribe",
    }


def test_secret_loader_fails_closed_and_never_transforms_values(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("fixture-value", encoding="utf-8")
    assert _read_required_secret(str(secret), "fixture") == "fixture-value"

    secret.write_text("contains whitespace", encoding="utf-8")
    with pytest.raises(SystemExit, match="contains whitespace"):
        _read_required_secret(str(secret), "fixture")
    with pytest.raises(SystemExit, match="not configured"):
        _read_required_secret(None, "fixture")


def _service_block(text: str, service_name: str) -> str:
    start = text.index(f"\n  {service_name}:\n")
    lines = text[start + 1 :].splitlines()
    body = [lines[0]]
    for line in lines[1:]:
        if line and not line.startswith("    ") and not line.startswith("  #"):
            break
        body.append(line)
    return "\n".join(body)


def test_radarbot_compose_service_is_profile_gated_hardened_and_isolated() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    service = _service_block(compose, "radarbot")
    instructions = "\n".join(
        line for line in service.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    assert 'profiles: ["radarbot"]' in service
    assert 'user: "1002:1002"' in service
    assert "read_only: true" in service
    assert 'cap_drop: ["ALL"]' in service
    assert 'security_opt: ["no-new-privileges:true"]' in service
    assert "radar-db-net" in service
    assert "radar-bot-egress" in service
    assert "radar-internal-bridge" not in instructions
    assert "radar-gateway-egress" not in instructions
    assert "radar-sidecar-egress" not in instructions
    assert "discord_commands_bot_token" in service
    assert "radarbot_database_url" in service
    assert "DISCORD_TOKEN=" not in service
    assert "DATABASE_URL=" not in service
    assert "apps.scheduler" not in service
    assert "crawler" not in instructions.lower()


def test_radarbot_image_has_one_non_root_bot_entrypoint_and_healthcheck() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    assert "FROM python@sha256:" in instructions
    assert "USER radarbot" in instructions
    assert "HEALTHCHECK" in instructions
    assert 'ENTRYPOINT ["python", "-m", "apps.discord_bot.container_main"]' in instructions
    assert "apps.scheduler.main" not in instructions


def test_app_role_bootstrap_is_password_parameterized_and_no_ddl_role() -> None:
    sql = BOOTSTRAP_SQL_PATH.read_text(encoding="utf-8")
    assert ":'app_password'" in sql
    assert "CHANGE_ME" not in sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS" in sql
    assert "REVOKE TEMP, CREATE ON DATABASE radar FROM radar_bot_app" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM radar_bot_app" in sql


def test_radarbot_local_secrets_are_gitignored() -> None:
    for name in ("discord_commands_bot_token", "radarbot_database_url"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(REPO_ROOT / "openab" / ".local" / name)],
            cwd=REPO_ROOT,
            timeout=10,
        )
        assert result.returncode == 0
