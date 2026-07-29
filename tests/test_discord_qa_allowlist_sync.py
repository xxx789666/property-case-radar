from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def load_module():
    path = REPO_ROOT / "scripts" / "sync_discord_qa_allowlist.py"
    spec = importlib.util.spec_from_file_location("discord_qa_allowlist_sync", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeApi:
    def __init__(self) -> None:
        self.assigned: list[str] = []

    def request(self, method: str, path: str, payload=None):
        if method == "GET" and path.endswith("/roles"):
            return [{"id": "role-1", "name": "Radar 問答", "managed": False}]
        if method == "GET" and "/members?" in path:
            return [
                {"user": {"id": "200", "bot": False}, "roles": []},
                {"user": {"id": "100", "bot": False}, "roles": ["role-1"]},
                {"user": {"id": "300", "bot": True}, "roles": []},
            ]
        if method == "PUT" and "/roles/role-1" in path:
            self.assigned.append(path.split("/members/")[1].split("/")[0])
            return None
        raise AssertionError((method, path, payload))


def test_sync_assigns_humans_and_writes_sorted_allowlist(tmp_path: Path) -> None:
    module = load_module()
    api = FakeApi()
    role_id_file = tmp_path / "role-id"
    allowlist_file = tmp_path / "users.json"

    result = module.sync(
        api=api,
        guild_id="guild-1",
        role_name="Radar 問答",
        role_id_file=role_id_file,
        allowlist_file=allowlist_file,
    )

    assert result["member_count"] == 2
    assert result["assigned_count"] == 1
    assert result["allowlist_changed"] is True
    assert api.assigned == ["200"]
    assert json.loads(allowlist_file.read_text(encoding="utf-8")) == ["100", "200"]
    assert role_id_file.read_text(encoding="ascii").strip() == "role-1"


def test_atomic_allowlist_write_reports_no_change(tmp_path: Path) -> None:
    module = load_module()
    path = tmp_path / "users.json"
    assert module.atomic_write_allowlist(path, ["100"]) is True
    assert module.atomic_write_allowlist(path, ["100"]) is False
