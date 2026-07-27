from __future__ import annotations

from pathlib import Path

from apps.scheduler.main import build_production_scheduler

ROOT = Path(__file__).resolve().parent.parent


def _service_block(text: str, service_name: str) -> str:
    start = text.index(f"\n  {service_name}:\n")
    lines = text[start + 1 :].splitlines()
    body = [lines[0]]
    for line in lines[1:]:
        if line and not line.startswith("    ") and not line.startswith("  #"):
            break
        body.append(line)
    return "\n".join(body)


def test_production_scheduler_registers_official_market_and_optional_auction_jobs() -> None:
    scheduler = build_production_scheduler(
        lambda: None,
        24,
        auction_job=lambda: None,
        auction_interval_hours=48,
    )
    assert {job.id for job in scheduler.get_jobs()} == {"market-price-sync", "auction-crawler"}
    assert scheduler.get_job("market-price-sync").max_instances == 1
    assert scheduler.get_job("auction-crawler").trigger.interval.total_seconds() == 48 * 3600


def test_production_sale_job_defaults_to_daily_interval() -> None:
    scheduler = build_production_scheduler(
        lambda: None,
        24,
        sale_job=lambda: None,
    )
    sale_job = scheduler.get_job("sale-crawler")
    assert sale_job is not None
    assert sale_job.trigger.interval.total_seconds() == 24 * 3600


def test_scheduler_compose_service_is_separate_hardened_and_secret_scoped() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service = _service_block(compose, "scheduler")
    instructions = "\n".join(
        line for line in service.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    assert 'profiles: ["scheduler"]' in service
    assert 'user: "1003:1003"' in service
    assert "read_only: true" in service
    assert 'cap_drop: ["ALL"]' in service
    assert 'security_opt: ["no-new-privileges:true"]' in service
    assert "radar-db-net" in service
    assert "radar-scheduler-egress" in service
    assert "radarbot_database_url" in service
    assert "discord_commands_bot_token" not in instructions
    assert "openab" not in instructions.lower()
    assert "radar-internal-bridge" not in instructions
    assert "DATABASE_URL=" not in instructions


def test_scheduler_image_has_fixed_non_root_entrypoint() -> None:
    dockerfile = (ROOT / "deploy" / "scheduler" / "Dockerfile").read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    assert "FROM python@sha256:" in instructions
    assert "USER radarscheduler" in instructions
    assert 'ENTRYPOINT ["python", "-m", "apps.scheduler.container_main"]' in instructions
    assert "HEALTHCHECK" in instructions
