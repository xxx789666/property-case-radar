"""Hardened file-secret entrypoint for the production scheduler container."""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import text

from apps.discord_bot.container_main import _read_required_secret


def _write_ready(path_text: str, *, auction_live_enabled: bool) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "status": "ready",
                "source": "moi-official-open-data",
                "auction_live_enabled": auction_live_enabled,
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main() -> None:
    os.environ["DATABASE_URL"] = _read_required_secret(
        os.environ.get("DATABASE_URL_SECRET_FILE"),
        "database URL",
    )

    from apps.config import get_settings
    from apps.scheduler.main import main as run_scheduler
    from database.session import create_db_engine

    settings = get_settings()
    # Remove the credential from the process environment immediately after
    # Settings has captured it; the SQLAlchemy engine retains only its parsed
    # connection URL internally and no secret is logged.
    os.environ.pop("DATABASE_URL", None)
    engine = create_db_engine(settings.database_url)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    _write_ready(
        os.environ.get("SCHEDULER_READY_FILE", "/run/radar-scheduler/ready.json"),
        auction_live_enabled=settings.auction_capture_enabled,
    )
    run_scheduler()


if __name__ == "__main__":
    main()
