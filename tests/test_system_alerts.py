import json
from pathlib import Path

from apps.config import Settings
from apps.services.system_alerts import send_discord_system_message, update_system_alert
from scripts.system_watchdog import latest_backup_is_fresh


def test_alerts_only_on_failure_and_recovery_transitions(tmp_path: Path):
    settings = Settings(
        database_url="sqlite://",
        system_alert_state_path=tmp_path / "state.json",
    )
    messages: list[str] = []
    sender = lambda message: messages.append(message) is None or True

    assert not update_system_alert(
        settings,
        key="service",
        failing=False,
        title="排程服務",
        detail="正常",
        sender=sender,
    )
    assert update_system_alert(
        settings,
        key="service",
        failing=True,
        title="排程服務",
        detail="無法連線",
        sender=sender,
    )
    assert not update_system_alert(
        settings,
        key="service",
        failing=True,
        title="排程服務",
        detail="無法連線",
        sender=sender,
    )
    assert update_system_alert(
        settings,
        key="service",
        failing=False,
        title="排程服務",
        detail="正常",
        sender=sender,
    )
    assert len(messages) == 2
    assert "故障" in messages[0]
    assert "已恢復" in messages[1]


def test_discord_system_message_preserves_chinese_as_ascii_json(monkeypatch):
    settings = Settings(
        database_url="sqlite://",
        discord_token="test-token",
        discord_system_alert_channel_id=123,
    )
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        captured["content_type"] = request.headers["Content-type"]
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    message = "🚨 排程工作 rental-crawler｜故障\n資料庫數值超出範圍"
    assert send_discord_system_message(settings, message)
    assert captured["body"].isascii()
    assert json.loads(captured["body"].decode("ascii"))["content"] == message
    assert captured["content_type"] == "application/json; charset=utf-8"
    assert captured["timeout"] == 15


def test_latest_backup_requires_recent_dump(tmp_path: Path):
    assert not latest_backup_is_fresh(tmp_path)
    (tmp_path / "radar_20260728_030000.dump").write_bytes(b"backup")
    assert latest_backup_is_fresh(tmp_path)
