from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.base import BaseScheduler

from apps.config import Settings
from apps.services.system_alerts import update_system_alert

logger = logging.getLogger(__name__)


class SchedulerJobRecovery:
    """Retry failed APScheduler jobs without shifting their daily cron time."""

    RETRY_PREFIX = "self-heal-retry:"

    def __init__(self, scheduler: BaseScheduler, settings: Settings) -> None:
        self.scheduler = scheduler
        self.settings = settings
        self.attempts: dict[str, int] = {}

    def attach(self) -> None:
        self.scheduler.add_listener(
            self.handle_event,
            EVENT_JOB_ERROR | EVENT_JOB_EXECUTED,
        )
        logger.info(
            "scheduler job self-healing enabled: retry_delay=%s minutes max_retries=%s",
            self.settings.self_heal_retry_delay_minutes,
            self.settings.self_heal_max_retries,
        )

    def handle_event(self, event) -> None:
        original_id = self._original_job_id(event.job_id)
        if event.exception is None:
            self._mark_recovered(original_id)
            return

        attempt = self.attempts.get(original_id, 0) + 1
        self.attempts[original_id] = attempt
        retry_scheduled = self._schedule_retry(original_id, attempt)
        detail = str(event.exception)[:1200]
        if retry_scheduled:
            detail += (
                f"\n自癒系統將於 {self.settings.self_heal_retry_delay_minutes} 分鐘後"
                f"進行第 {attempt}/{self.settings.self_heal_max_retries} 次補跑。"
            )
        else:
            detail += (
                f"\n自癒補跑已達上限 "
                f"{self.settings.self_heal_max_retries} 次，需要人工處理。"
            )

        update_system_alert(
            self.settings,
            key=f"scheduler-job:{original_id}",
            failing=True,
            title=f"排程工作 {original_id}",
            detail=detail,
        )
        update_system_alert(
            self.settings,
            key=f"scheduler-retry-exhausted:{original_id}",
            failing=not retry_scheduled,
            title=f"排程自癒 {original_id}",
            detail=detail,
        )

    def _schedule_retry(self, original_id: str, attempt: int) -> bool:
        if attempt > self.settings.self_heal_max_retries:
            return False
        original = self.scheduler.get_job(original_id)
        if original is None:
            logger.error("cannot self-heal missing scheduler job: %s", original_id)
            return False

        run_at = datetime.now(self.scheduler.timezone) + timedelta(
            minutes=self.settings.self_heal_retry_delay_minutes
        )
        self.scheduler.add_job(
            original.func,
            "date",
            run_date=run_at,
            id=f"{self.RETRY_PREFIX}{original_id}",
            args=original.args,
            kwargs=original.kwargs,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        logger.warning(
            "scheduled self-heal retry %s/%s for %s at %s",
            attempt,
            self.settings.self_heal_max_retries,
            original_id,
            run_at,
        )
        return True

    def _mark_recovered(self, original_id: str) -> None:
        prior_attempts = self.attempts.pop(original_id, 0)
        retry_id = f"{self.RETRY_PREFIX}{original_id}"
        if not str(original_id).startswith(self.RETRY_PREFIX):
            pending = self.scheduler.get_job(retry_id)
            if pending is not None:
                self.scheduler.remove_job(retry_id)
        update_system_alert(
            self.settings,
            key=f"scheduler-job:{original_id}",
            failing=False,
            title=f"排程工作 {original_id}",
            detail=(
                f"{original_id} 自動補跑成功，已恢復正常。"
                if prior_attempts
                else f"{original_id} 已正常完成。"
            ),
        )
        update_system_alert(
            self.settings,
            key=f"scheduler-retry-exhausted:{original_id}",
            failing=False,
            title=f"排程自癒 {original_id}",
            detail=f"{original_id} 已恢復正常。",
        )

    def _original_job_id(self, job_id: str) -> str:
        if job_id.startswith(self.RETRY_PREFIX):
            return job_id.removeprefix(self.RETRY_PREFIX)
        return job_id
