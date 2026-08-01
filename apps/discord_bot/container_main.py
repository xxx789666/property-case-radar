"""File-secret loader for the hardened RadarBot container.

The two credentials enter the container only as read-only Docker secret
files. They are copied into this already-running Python process's
environment just long enough for pydantic-settings to build its cached
Settings object; apps.discord_bot.main removes both entries immediately
afterward. They are never command-line arguments, Compose environment
values, or log fields.
"""

from __future__ import annotations

import os
from pathlib import Path


def _read_required_secret(path_text: str | None, label: str) -> str:
    if not path_text:
        raise SystemExit(f"{label} secret path is not configured")
    path = Path(path_text)
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise SystemExit(f"{label} secret file is not readable") from None
    if not value or any(character.isspace() for character in value):
        raise SystemExit(f"{label} secret file is empty or contains whitespace")
    return value


def main() -> None:
    os.environ["DISCORD_TOKEN"] = _read_required_secret(
        os.environ.get("DISCORD_TOKEN_SECRET_FILE"),
        "Discord token",
    )
    os.environ["DATABASE_URL"] = _read_required_secret(
        os.environ.get("DATABASE_URL_SECRET_FILE"),
        "database URL",
    )

    from apps.discord_bot.main import main as run_bot

    run_bot()


if __name__ == "__main__":
    main()
