"""Static checks for the native Windows OpenAB deployment."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_CONFIG = REPO_ROOT / "openab" / "windows" / "config-radar-agent.toml"


def test_windows_config_uses_mention_to_thread_flow_and_local_bridge() -> None:
    parsed = tomllib.loads(WINDOWS_CONFIG.read_text(encoding="utf-8"))
    discord = parsed["discord"]
    assert set(discord["allowed_channels"]) == {"1530076451242508318", "1530076529751756870"}
    assert discord["allowed_users"] == [
        "843428445802725388",
        "1149306408085508106",
    ]
    assert discord["allowed_role_ids"] == ["1530137617952411779"]
    assert discord["allow_dm"] is False
    assert discord["allow_bot_messages"] == "off"
    assert discord["allow_user_messages"] == "involved"
    assert discord["message_processing_mode"] == "per-thread"

    agent = parsed["agent"]
    assert agent["command"] == "node"
    assert agent["args"] == ["${RADAR_BRIDGE_CLIENT}"]
    assert agent["working_dir"] == "${RADAR_AGENT_WORKING_DIR}"
    assert agent["env"]["ACP_SIDECAR_HOST"] == "127.0.0.1"
    assert agent["env"]["ACP_SIDECAR_PORT"] == "18765"
    assert "DATABASE_URL" not in str(parsed)
    assert parsed["pool"]["default_config_options"]["mode"] == "agent-full-access"
    assert parsed["reactions"]["tool_display"] == "none"


def test_agent_contract_forbids_direct_database_and_intermediate_answers() -> None:
    contract = (REPO_ROOT / "openab" / "sidecar" / "AGENTS.md").read_text(encoding="utf-8")
    assert "python -X utf8 -m tools.radar_agent_query" in contract
    assert "Never issue SQL" in contract
    assert "Never read `.env`" in contract
    assert "Emit exactly one user-facing conclusion" in contract
    assert "Do not retry" in contract
    assert "type_a_building_land" in contract
    assert "type_d_building_land" in contract


def test_agent_contract_uses_actual_case_type_official_url_and_original_pdf() -> None:
    contract = (REPO_ROOT / "openab" / "sidecar" / "AGENTS.md").read_text(encoding="utf-8")
    assert "未指定類型時不得擅自加入類型篩選" in contract
    assert "實際的 `case_type`" in contract
    assert "查看拍賣公告：[官方網址]" in contract
    assert "法院原始 PDF：" in contract
    assert "python -X utf8 -m tools.radar_agent_pdf upload" in contract
    assert "禁止建立、渲染或提供摘要 PDF" in contract


def test_windows_wrappers_start_restricted_pdf_broker_without_exposing_token_to_sidecar() -> None:
    gateway = (REPO_ROOT / "scripts" / "run_openab_gateway.ps1").read_text(encoding="utf-8")
    sidecar = (REPO_ROOT / "scripts" / "run_openab_sidecar.ps1").read_text(encoding="utf-8")
    assert "pdf-upload-broker.mjs" in gateway
    assert "RADAR_PDF_ALLOWED_PARENT_IDS" in gateway
    assert "$brokerPortReleased" in gateway
    assert "Previous PDF upload broker did not release port 18766" in gateway
    assert 'Remove-Item Env:OPENAB_DISCORD_BOT_TOKEN' in gateway
    assert 'RADAR_PDF_UPLOAD_BROKER_URL' in sidecar
    assert 'RADAR_AUCTION_DOWNLOAD_DIR' in sidecar
    assert 'Python313\\python.exe' in sidecar
    assert '$env:PATH = "$(Split-Path -Parent $pythonExe);$env:PATH"' in sidecar
    assert "OPENAB_DISCORD_BOT_TOKEN" not in sidecar


def test_windows_gateway_syncs_discord_qa_role_allowlist_before_start() -> None:
    gateway = (REPO_ROOT / "scripts" / "run_openab_gateway.ps1").read_text(encoding="utf-8")
    runner = (REPO_ROOT / "scripts" / "run_discord_qa_allowlist_sync.ps1").read_text(
        encoding="utf-8"
    )
    register = (REPO_ROOT / "scripts" / "register_discord_qa_allowlist_task.ps1").read_text(
        encoding="utf-8"
    )
    assert "sync_discord_qa_allowlist.py" in gateway
    assert "discord-qa-allowed-users.json" in gateway
    assert "$allowlistFile" in gateway
    assert "--changed-exit-code 10" in runner
    assert "--verification-channel-id-file" in runner
    assert "--verification-message-id-file" in runner
    assert "Property Case Radar OpenAB Gateway" in runner
    assert "New-TimeSpan -Minutes 5" in register


def test_windows_tasks_start_only_openab_not_legacy_commands_bot() -> None:
    register = (REPO_ROOT / "scripts" / "register_openab_tasks.ps1").read_text(encoding="utf-8")
    assert "Property Case Radar OpenAB Gateway" in register
    assert "Property Case Radar OpenAB Sidecar" in register
    assert "apps.discord_bot" not in register
    assert "radarbot" not in register.lower()


def test_runtime_artifacts_and_secrets_are_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "openab/.local/" in gitignore
    assert "openab/.runtime/" in gitignore
