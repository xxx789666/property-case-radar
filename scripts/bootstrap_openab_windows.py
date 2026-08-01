"""Bootstrap native-Windows OpenAB secrets without printing either secret."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path

import requests
from sqlalchemy import URL

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "openab" / ".local"
BOT_TOKEN_SOURCE = Path(r"D:\bot.txt")
OPENAB_APPLICATION_ID = "1530136356439719997"
TOKEN_PATTERN = re.compile(r"([A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,})")
PSQL = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")


def _select_openab_token() -> str:
    candidates = TOKEN_PATTERN.findall(BOT_TOKEN_SOURCE.read_text(encoding="utf-8-sig", errors="ignore"))
    for token in dict.fromkeys(candidates):
        response = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=15,
        )
        if response.status_code == 200 and response.json().get("id") == OPENAB_APPLICATION_ID:
            return token
    raise RuntimeError(f"No valid Discord token for application {OPENAB_APPLICATION_ID} was found")


def _bootstrap_database(password: str) -> None:
    admin_secret = LOCAL_DIR / "postgres_admin_password"
    if not admin_secret.exists():
        raise RuntimeError(f"PostgreSQL administrator secret is missing: {admin_secret}")
    env = os.environ.copy()
    env["PGPASSWORD"] = admin_secret.read_text(encoding="utf-8").strip()
    command = [
        str(PSQL),
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        "postgres",
        "-d",
        "radar",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"ro_password={password}",
        "-f",
        str(REPO_ROOT / "openab" / "sidecar" / "bootstrap_read_only_role.sql"),
    ]
    try:
        subprocess.run(command, env=env, check=True, stdout=subprocess.DEVNULL)
    finally:
        env.pop("PGPASSWORD", None)


def _restrict_to_current_user(path: Path) -> None:
    username = os.environ.get("USERNAME")
    if username:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(F)"],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def main() -> int:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    token_path = LOCAL_DIR / "openab_discord_bot_token"
    db_path = LOCAL_DIR / "radar_agent_database_url"

    token_path.write_text(_select_openab_token(), encoding="utf-8")
    _restrict_to_current_user(token_path)

    role_password = secrets.token_urlsafe(32)
    _bootstrap_database(role_password)
    database_url = URL.create(
        "postgresql+psycopg",
        username="radar_agent_ro",
        password=role_password,
        host="127.0.0.1",
        port=5432,
        database="radar",
    ).render_as_string(hide_password=False)
    db_path.write_text(database_url, encoding="utf-8")
    _restrict_to_current_user(db_path)
    print("OpenAB Discord token and read-only radar database role are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
