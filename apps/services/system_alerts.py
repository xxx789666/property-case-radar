from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from apps.config import Settings

logger = logging.getLogger(__name__)
AlertSender = Callable[[str], bool]


def send_discord_system_message(settings: Settings, message: str) -> bool:
    if not settings.discord_token or not settings.discord_system_alert_channel_id:
        logger.error("system alert could not be sent: Discord is not configured")
        return False
    request = urllib.request.Request(
        (
            "https://discord.com/api/v10/channels/"
            f"{settings.discord_system_alert_channel_id}/messages"
        ),
        data=json.dumps({"content": message}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bot {settings.discord_token}",
            "Content-Type": "application/json",
            "User-Agent": "PropertyCaseRadar/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.HTTPError) as error:
        logger.error("system alert Discord delivery failed: %s", error)
        return False


def _read_state(path: Path) -> dict[str, bool]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): bool(status) for key, status in value.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return {}


def _write_state(path: Path, state: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def update_system_alert(
    settings: Settings,
    *,
    key: str,
    failing: bool,
    title: str,
    detail: str,
    sender: AlertSender | None = None,
) -> bool:
    """Send only state transitions so a five-minute watchdog cannot spam."""
    state_path = settings.system_alert_state_path
    if not state_path.is_absolute():
        state_path = Path.cwd() / state_path
    state = _read_state(state_path)
    previous = state.get(key, False)
    if previous == failing:
        return False

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    marker = "🚨" if failing else "✅"
    status = "故障" if failing else "已恢復"
    content = f"{marker} **{title}｜{status}**\n{detail}\n時間：{timestamp}"
    delivered = (sender or (lambda message: send_discord_system_message(settings, message)))(
        content
    )
    if delivered:
        state[key] = failing
        _write_state(state_path, state)
    return delivered
