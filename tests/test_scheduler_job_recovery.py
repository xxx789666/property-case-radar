from types import SimpleNamespace
from zoneinfo import ZoneInfo

from apps.config import Settings
from apps.services.scheduler_job_recovery import SchedulerJobRecovery


class FakeScheduler:
    timezone = ZoneInfo("Asia/Taipei")

    def __init__(self):
        self.jobs = {
            "rental-crawler": SimpleNamespace(
                func=lambda: None,
                args=(),
                kwargs={},
            )
        }
        self.added = []
        self.removed = []
        self.listener = None

    def add_listener(self, listener, mask):
        self.listener = listener
        self.listener_mask = mask

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger, **kwargs):
        self.added.append((func, trigger, kwargs))
        self.jobs[kwargs["id"]] = SimpleNamespace(
            func=func,
            args=kwargs.get("args", ()),
            kwargs=kwargs.get("kwargs", {}),
        )

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


def test_failed_job_schedules_bounded_retry(monkeypatch, tmp_path):
    scheduler = FakeScheduler()
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
        self_heal_retry_delay_minutes=15,
        self_heal_max_retries=3,
    )
    alerts = []
    monkeypatch.setattr(
        "apps.services.scheduler_job_recovery.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )
    recovery = SchedulerJobRecovery(scheduler, settings)

    recovery.handle_event(
        SimpleNamespace(job_id="rental-crawler", exception=RuntimeError("boom"))
    )

    assert recovery.attempts["rental-crawler"] == 1
    assert scheduler.added[0][1] == "date"
    assert scheduler.added[0][2]["id"] == "self-heal-retry:rental-crawler"
    assert "第 1/3 次補跑" in alerts[0]["detail"]


def test_successful_retry_clears_failure_and_counter(monkeypatch, tmp_path):
    scheduler = FakeScheduler()
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
    )
    alerts = []
    monkeypatch.setattr(
        "apps.services.scheduler_job_recovery.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )
    recovery = SchedulerJobRecovery(scheduler, settings)
    recovery.attempts["rental-crawler"] = 1

    recovery.handle_event(
        SimpleNamespace(
            job_id="self-heal-retry:rental-crawler",
            exception=None,
        )
    )

    assert "rental-crawler" not in recovery.attempts
    assert any(
        alert["key"] == "scheduler-job:rental-crawler"
        and alert["failing"] is False
        for alert in alerts
    )


def test_retry_stops_after_configured_limit(monkeypatch, tmp_path):
    scheduler = FakeScheduler()
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
        self_heal_max_retries=1,
    )
    alerts = []
    monkeypatch.setattr(
        "apps.services.scheduler_job_recovery.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )
    recovery = SchedulerJobRecovery(scheduler, settings)

    recovery.handle_event(
        SimpleNamespace(job_id="rental-crawler", exception=RuntimeError("first"))
    )
    recovery.handle_event(
        SimpleNamespace(
            job_id="self-heal-retry:rental-crawler",
            exception=RuntimeError("second"),
        )
    )

    assert len(scheduler.added) == 1
    assert any(
        alert["key"] == "scheduler-retry-exhausted:rental-crawler"
        and alert["failing"] is True
        for alert in alerts
    )


def test_nonretryable_failure_does_not_schedule_job_retry(monkeypatch, tmp_path):
    scheduler = FakeScheduler()
    settings = Settings(
        database_url="sqlite://",
        discord_token=None,
        system_alert_state_path=tmp_path / "alerts.json",
    )
    alerts = []
    monkeypatch.setattr(
        "apps.services.scheduler_job_recovery.update_system_alert",
        lambda _settings, **kwargs: alerts.append(kwargs),
    )

    class NetworkBlocked(RuntimeError):
        retryable = False

    recovery = SchedulerJobRecovery(scheduler, settings)
    recovery.handle_event(
        SimpleNamespace(
            job_id="rental-crawler",
            exception=NetworkBlocked("socket 10013"),
        )
    )

    assert scheduler.added == []
    assert "rental-crawler" not in recovery.attempts
    assert "停止無效補跑" in alerts[0]["detail"]
