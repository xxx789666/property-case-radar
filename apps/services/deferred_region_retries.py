"""Schedule failed-region retries as independent APScheduler date jobs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.base import BaseScheduler

from apps.services.performance import measure_stage


logger = logging.getLogger(__name__)
RetryCallback = Callable[[tuple[str, ...], int], tuple[str, ...]]


class DeferredRegionRetryScheduler:
    """Bound after scheduler construction to avoid circular job wiring."""

    def __init__(self) -> None:
        self.scheduler: BaseScheduler | None = None

    def bind(self, scheduler: BaseScheduler) -> None:
        self.scheduler = scheduler

    def schedule(
        self,
        *,
        key: str,
        regions: tuple[str, ...],
        callback: RetryCallback,
        delay_minutes: int,
        max_attempts: int,
        attempt: int = 1,
    ) -> bool:
        if not regions or max_attempts < 1 or attempt > max_attempts:
            return False
        if self.scheduler is None:
            raise RuntimeError("deferred region retry scheduler is not bound")

        run_at = datetime.now(self.scheduler.timezone) + timedelta(
            minutes=delay_minutes
        )

        def run_once() -> None:
            unresolved = regions
            try:
                with measure_stage(
                    logger,
                    "retry",
                    key,
                    items=len(regions),
                ):
                    unresolved = tuple(callback(regions, attempt))
            except Exception:  # noqa: BLE001 - next attempt remains bounded
                logger.exception(
                    "deferred region retry failed: key=%s attempt=%s/%s",
                    key,
                    attempt,
                    max_attempts,
                )
            if unresolved and attempt < max_attempts:
                self.schedule(
                    key=key,
                    regions=unresolved,
                    callback=callback,
                    delay_minutes=delay_minutes,
                    max_attempts=max_attempts,
                    attempt=attempt + 1,
                )
            elif unresolved:
                logger.error(
                    "deferred region retries exhausted: key=%s regions=%s",
                    key,
                    ", ".join(unresolved),
                )

        self.scheduler.add_job(
            run_once,
            "date",
            run_date=run_at,
            id=f"region-retry:{key}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            misfire_grace_time=max(60, delay_minutes * 60),
        )
        logger.warning(
            "scheduled deferred region retry: key=%s attempt=%s/%s run_at=%s regions=%s",
            key,
            attempt,
            max_attempts,
            run_at.isoformat(),
            ", ".join(regions),
        )
        return True
