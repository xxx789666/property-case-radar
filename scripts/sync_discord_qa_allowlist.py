#!/usr/bin/env python3
"""Build OpenAB's user allowlist from verified Radar Q&A role members."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_GUILD_ID = "1530072733818556538"
DEFAULT_ROLE_NAME = "Radar 問答"


class DiscordApiError(RuntimeError):
    pass


class DiscordApi:
    def __init__(self, token: str) -> None:
        self.headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "PropertyCaseRadar/1.0",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        for attempt in range(4):
            request = Request(
                DISCORD_API_BASE + path,
                data=data,
                headers=self.headers,
                method=method,
            )
            try:
                with urlopen(request, timeout=20) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except HTTPError as exc:
                response_body = exc.read()
                if exc.code == 429 and attempt < 3:
                    details = json.loads(response_body or b"{}")
                    time.sleep(float(details.get("retry_after", 1)))
                    continue
                detail = response_body.decode("utf-8", errors="replace")[:500]
                raise DiscordApiError(
                    f"Discord API {method} {path} failed: HTTP {exc.code}: {detail}"
                ) from exc
        raise DiscordApiError(f"Discord API {method} {path} exhausted retries")


def list_all_members(api: DiscordApi, guild_id: str) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        path = f"/guilds/{guild_id}/members?limit=1000"
        if after:
            path += f"&after={after}"
        page = api.request("GET", path)
        if not isinstance(page, list):
            raise DiscordApiError("Discord member-list response was not a list")
        members.extend(page)
        if len(page) < 1000:
            return members
        after = str(page[-1]["user"]["id"])


def list_reaction_users(
    api: DiscordApi,
    channel_id: str,
    message_id: str,
    emoji: str = "✅",
) -> set[str]:
    user_ids: set[str] = set()
    after: str | None = None
    encoded_emoji = quote(emoji, safe="")
    while True:
        path = (
            f"/channels/{channel_id}/messages/{message_id}/reactions/"
            f"{encoded_emoji}?limit=100"
        )
        if after:
            path += f"&after={after}"
        page = api.request("GET", path)
        if not isinstance(page, list):
            raise DiscordApiError("Discord reaction-user response was not a list")
        user_ids.update(str(user["id"]) for user in page if not user.get("bot"))
        if len(page) < 100:
            return user_ids
        after = str(page[-1]["id"])


def ensure_role(api: DiscordApi, guild_id: str, role_name: str, role_id_file: Path) -> str:
    roles = api.request("GET", f"/guilds/{guild_id}/roles")
    if not isinstance(roles, list):
        raise DiscordApiError("Discord role-list response was not a list")

    saved_role_id = (
        role_id_file.read_text(encoding="ascii").strip()
        if role_id_file.exists()
        else ""
    )
    role = next(
        (
            item
            for item in roles
            if not item.get("managed")
            and (
                str(item.get("id")) == saved_role_id
                or (not saved_role_id and item.get("name") == role_name)
            )
        ),
        None,
    )
    if role is None:
        role = api.request(
            "POST",
            f"/guilds/{guild_id}/roles",
            {
                "name": role_name,
                "permissions": "0",
                "color": 3447003,
                "hoist": False,
                "mentionable": False,
            },
        )
    role_id = str(role["id"])
    role_id_file.parent.mkdir(parents=True, exist_ok=True)
    role_id_file.write_text(role_id + "\n", encoding="ascii")
    return role_id


def atomic_write_allowlist(path: Path, user_ids: list[str]) -> bool:
    rendered = json.dumps(user_ids, ensure_ascii=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)
    return True


def sync(
    *,
    api: DiscordApi,
    guild_id: str,
    role_name: str,
    role_id_file: Path,
    allowlist_file: Path,
    verification_channel_id_file: Path | None = None,
    verification_message_id_file: Path | None = None,
) -> dict[str, Any]:
    role_id = ensure_role(api, guild_id, role_name, role_id_file)
    members = list_all_members(api, guild_id)
    newly_verified: set[str] = set()

    if (
        verification_channel_id_file
        and verification_message_id_file
        and verification_channel_id_file.exists()
        and verification_message_id_file.exists()
    ):
        channel_id = verification_channel_id_file.read_text(encoding="ascii").strip()
        message_id = verification_message_id_file.read_text(encoding="ascii").strip()
        reacted_user_ids = list_reaction_users(api, channel_id, message_id)
        for member in members:
            user = member.get("user", {})
            user_id = str(user.get("id", ""))
            member_roles = {str(item) for item in member.get("roles", [])}
            if (
                not user.get("bot")
                and user_id in reacted_user_ids
                and role_id not in member_roles
            ):
                api.request(
                    "PUT",
                    f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
                )
                newly_verified.add(user_id)

    verified_members = [
        member
        for member in members
        if not member.get("user", {}).get("bot")
        and (
            role_id in {str(item) for item in member.get("roles", [])}
            or str(member["user"]["id"]) in newly_verified
        )
    ]
    allowed_users = sorted(str(member["user"]["id"]) for member in verified_members)
    allowlist_changed = atomic_write_allowlist(allowlist_file, allowed_users)
    return {
        "ok": True,
        "guild_id": guild_id,
        "role_id": role_id,
        "member_count": len(allowed_users),
        "verified_member_count": len(allowed_users),
        "newly_verified_count": len(newly_verified),
        "allowlist_changed": allowlist_changed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--role-id-file", type=Path, required=True)
    parser.add_argument("--allowlist-file", type=Path, required=True)
    parser.add_argument("--guild-id", default=DEFAULT_GUILD_ID)
    parser.add_argument("--role-name", default=DEFAULT_ROLE_NAME)
    parser.add_argument("--verification-channel-id-file", type=Path)
    parser.add_argument("--verification-message-id-file", type=Path)
    parser.add_argument("--changed-exit-code", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("Discord token file is empty or malformed")
    result = sync(
        api=DiscordApi(token),
        guild_id=args.guild_id,
        role_name=args.role_name,
        role_id_file=args.role_id_file,
        allowlist_file=args.allowlist_file,
        verification_channel_id_file=args.verification_channel_id_file,
        verification_message_id_file=args.verification_message_id_file,
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["allowlist_changed"] and args.changed_exit_code:
        return args.changed_exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
