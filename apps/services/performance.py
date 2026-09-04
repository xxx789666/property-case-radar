"""Small structured wall-clock timers used by scheduler and crawler stages."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Iterator


@dataclass
class StageMeasurement:
    items: int | None = None


@contextmanager
def measure_stage(
    logger: logging.Logger,
    stage: str,
    source: str,
    *,
    items: int | None = None,
) -> Iterator[StageMeasurement]:
    """Log one stable, machine-searchable timing record on exit."""

    measurement = StageMeasurement(items=items)
    started = perf_counter()
    status = "ok"
    try:
        yield measurement
    except BaseException:
        status = "error"
        raise
    finally:
        duration = perf_counter() - started
        logger.info(
            "performance stage=%s source=%s duration_seconds=%.3f items=%s status=%s",
            stage,
            source,
            duration,
            measurement.items if measurement.items is not None else "unknown",
            status,
        )
