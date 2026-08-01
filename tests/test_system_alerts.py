from pathlib import Path

from apps.config import Settings
from apps.services.system_alerts import update_system_alert
from scripts.system_watchdog import latest_backup_is_fresh


def test_alerts_only_on_failure_and_recovery_transitions(tmp_path: Path):
    settings = Settings(
        database_url="sqlite://",
        system_alert_state_path=tmp_path / "state.json",
    )
    messages: list[str] = []
    sender = lambda message: messages.append(message) is None or True

    assert not update_system_alert(
        settings, key="service", failing=False, title="服務", detail="ok", sender=sender
    )
    assert update_system_alert(
        settings,
        key="service",
        failing=True,
        title="服務",
        detail="down",
        sender=sender,
    )
    assert not update_system_alert(
        settings,
        key="service",
        failing=True,
        title="服務",
        detail="down",
        sender=sender,
    )
    assert update_system_alert(
        settings, key="service", failing=False, title="服務", detail="ok", sender=sender
    )
    assert len(messages) == 2
    assert "故障" in messages[0]
    assert "已恢復" in messages[1]


def test_latest_backup_requires_recent_dump(tmp_path: Path):
    assert not latest_backup_is_fresh(tmp_path)
    (tmp_path / "radar_20260728_030000.dump").write_bytes(b"backup")
    assert latest_backup_is_fresh(tmp_path)
