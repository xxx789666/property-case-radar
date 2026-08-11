from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class SchedulerAlreadyRunning(RuntimeError):
    pass


def _try_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def scheduler_instance_lock(path: Path) -> Iterator[None]:
    """Hold an OS lock so orphaned task processes cannot duplicate the scheduler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        _try_lock(handle)
    except OSError as error:
        handle.close()
        raise SchedulerAlreadyRunning(
            "another Property Case Radar scheduler instance is already running"
        ) from error
    try:
        yield
    finally:
        _unlock(handle)
        handle.close()
