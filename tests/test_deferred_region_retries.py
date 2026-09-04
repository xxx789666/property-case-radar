from datetime import timezone

from apps.services.deferred_region_retries import DeferredRegionRetryScheduler


class FakeScheduler:
    timezone = timezone.utc

    def __init__(self) -> None:
        self.jobs: list[tuple[object, str, dict[str, object]]] = []

    def add_job(self, callback, trigger: str, **kwargs: object) -> None:
        self.jobs.append((callback, trigger, kwargs))


def test_deferred_retry_uses_bounded_date_jobs() -> None:
    scheduler = FakeScheduler()
    retries = DeferredRegionRetryScheduler()
    retries.bind(scheduler)  # type: ignore[arg-type]
    attempts: list[tuple[tuple[str, ...], int]] = []

    def callback(regions: tuple[str, ...], attempt: int) -> tuple[str, ...]:
        attempts.append((regions, attempt))
        return regions if attempt == 1 else ()

    assert retries.schedule(
        key="sale-591",
        regions=("臺北市",),
        callback=callback,
        delay_minutes=15,
        max_attempts=3,
    )
    first, trigger, kwargs = scheduler.jobs.pop(0)
    assert trigger == "date"
    assert kwargs["id"] == "region-retry:sale-591"
    first()
    second, trigger, _ = scheduler.jobs.pop(0)
    assert trigger == "date"
    second()
    assert attempts == [(("臺北市",), 1), (("臺北市",), 2)]
    assert scheduler.jobs == []
