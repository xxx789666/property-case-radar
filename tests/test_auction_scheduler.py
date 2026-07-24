from apps.scheduler.main import build_scheduler


def test_scheduler_registers_sale_job_only_by_default() -> None:
    scheduler = build_scheduler(lambda: None, 90)
    assert scheduler.get_job("sale-crawler") is not None
    assert scheduler.get_job("auction-crawler") is None


def test_scheduler_registers_both_jobs_when_auction_job_given() -> None:
    scheduler = build_scheduler(lambda: None, 90, auction_job=lambda: None, auction_interval_hours=8)
    sale_job = scheduler.get_job("sale-crawler")
    auction_job = scheduler.get_job("auction-crawler")
    assert sale_job is not None
    assert auction_job is not None
    assert sale_job.max_instances == 1
    assert auction_job.max_instances == 1


def test_auction_job_defaults_to_eight_hour_interval_when_unset() -> None:
    scheduler = build_scheduler(lambda: None, 90, auction_job=lambda: None)
    job = scheduler.get_job("auction-crawler")
    assert job.trigger.interval.total_seconds() == 8 * 3600
