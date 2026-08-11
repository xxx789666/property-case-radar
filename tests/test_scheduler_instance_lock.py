from __future__ import annotations

from pathlib import Path

import pytest

from apps.scheduler.instance_lock import (
    SchedulerAlreadyRunning,
    scheduler_instance_lock,
)


def test_scheduler_instance_lock_rejects_a_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"

    with scheduler_instance_lock(lock_path):
        with pytest.raises(SchedulerAlreadyRunning):
            with scheduler_instance_lock(lock_path):
                pass

    with scheduler_instance_lock(lock_path):
        pass
