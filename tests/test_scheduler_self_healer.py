from datetime import UTC, datetime, timedelta

from apps.config import Settings
from scripts.scheduler_self_healer import heal_component, reserve_attempt


def make_settings(tmp_path, **overrides):
    return Settings(
        database_url="sqlite://",
        discord_token=None,
        self_heal_state_path=tmp_path / "self-heal.json",
        self_heal_service_cooldown_minutes=10,
        self_heal_service_attempt_window_minutes=60,
        self_heal_max_retries=3,
        **overrides,
    )


def test_attempt_budget_enforces_cooldown_and_limit(tmp_path):
    settings = make_settings(tmp_path)
    state = {}
    now = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)

    assert reserve_attempt(state, "scheduler", settings, now=now) == 1
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=5),
        )
        is None
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=10),
        )
        == 2
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=20),
        )
        == 3
    )
    assert (
        reserve_attempt(
            state,
            "scheduler",
            settings,
            now=now + timedelta(minutes=30),
        )
        is None
    )


def test_successful_repair_clears_attempt_state(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    state = {}
    ready = False
    notifications = []
    alerts = []

    def repair():
        nonlocal ready
        ready = True

    monkeypatch.setattr(
        "scripts.scheduler_self_healer.send_discord_system_message",
        lambda _settings, message: notifications.append(message) or True,
    )
    monkeypatch.setattr(
        "scripts.scheduler_self_healer.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )

    healthy, attempted = heal_component(
        settings=settings,
        state=state,
        key="scheduler",
        title="Scheduler",
        healthy=lambda: ready,
        repair=repair,
        wait_seconds=1,
        now=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
    )

    assert healthy
    assert attempted
    assert "scheduler" not in state
    assert "已恢復" in notifications[0]
    assert alerts[-1]["failing"] is False
