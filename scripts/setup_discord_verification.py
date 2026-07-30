#!/usr/bin/env python3
"""Create the Discord verification gate and lock non-verification channels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sync_discord_qa_allowlist import DiscordApi, DiscordApiError

GUILD_ID = "1530072733818556538"
TEXT_CATEGORY_ID = "1530072733818556539"
LOCKED_CATEGORY_IDS = (
    TEXT_CATEGORY_ID,
    "1530074284104482917",
    "1530075486963892264",
    "1530072733818556540",
    "1532276240319516824",
)
BOT_ROLE_ID = "1530137617952411779"
ROLE_NAME = "Radar 問答"
CHANNEL_NAME = "驗證"
MESSAGE_MARKER = "property-case-radar-verification-v1"

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
READ_MESSAGE_HISTORY = 1 << 16
ADD_REACTIONS = 1 << 6


def write_id(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="ascii")


def find_role_id(roles: list[dict[str, Any]], saved_role_id: str) -> str:
    role = next(
        (
            item
            for item in roles
            if not item.get("managed")
            and (
                str(item["id"]) == saved_role_id
                or (not saved_role_id and item.get("name") == ROLE_NAME)
            )
        ),
        None,
    )
    if role is None:
        raise RuntimeError(f"Discord role {ROLE_NAME!r} does not exist")
    return str(role["id"])


def merged_overwrite(
    channel: dict[str, Any],
    overwrite_id: str,
    *,
    allow_bits: int = 0,
    deny_bits: int = 0,
) -> dict[str, Any]:
    current = next(
        (
            item
            for item in channel.get("permission_overwrites", [])
            if str(item["id"]) == overwrite_id
        ),
        None,
    )
    allow = int(current["allow"]) if current else 0
    deny = int(current["deny"]) if current else 0
    allow = (allow | allow_bits) & ~deny_bits
    deny = (deny | deny_bits) & ~allow_bits
    return {"type": 0, "allow": str(allow), "deny": str(deny)}


def put_overwrite(
    api: DiscordApi,
    channel: dict[str, Any],
    overwrite_id: str,
    *,
    allow_bits: int = 0,
    deny_bits: int = 0,
) -> None:
    api.request(
        "PUT",
        f"/channels/{channel['id']}/permissions/{overwrite_id}",
        merged_overwrite(
            channel,
            overwrite_id,
            allow_bits=allow_bits,
            deny_bits=deny_bits,
        ),
    )


def ensure_verification_channel(
    api: DiscordApi,
    channels: list[dict[str, Any]],
    role_id: str,
    channel_id_file: Path,
) -> dict[str, Any]:
    saved_channel_id = (
        channel_id_file.read_text(encoding="ascii").strip()
        if channel_id_file.exists()
        else ""
    )
    channel = next(
        (
            item
            for item in channels
            if item.get("type") == 0
            and (
                str(item["id"]) == saved_channel_id
                or (
                    not saved_channel_id
                    and item.get("name") == CHANNEL_NAME
                    and item.get("parent_id") == TEXT_CATEGORY_ID
                )
            )
        ),
        None,
    )
    if channel is None:
        channel = api.request(
            "POST",
            f"/guilds/{GUILD_ID}/channels",
            {
                "name": CHANNEL_NAME,
                "type": 0,
                "parent_id": TEXT_CATEGORY_ID,
                "position": 0,
                "topic": "閱讀規則並點擊 ✅，完成後將自動解鎖伺服器。",
                "permission_overwrites": [
                    {
                        "id": GUILD_ID,
                        "type": 0,
                        "allow": str(VIEW_CHANNEL | READ_MESSAGE_HISTORY | ADD_REACTIONS),
                        "deny": str(SEND_MESSAGES),
                    },
                    {
                        "id": BOT_ROLE_ID,
                        "type": 0,
                        "allow": str(
                            VIEW_CHANNEL
                            | SEND_MESSAGES
                            | READ_MESSAGE_HISTORY
                            | ADD_REACTIONS
                            | MANAGE_MESSAGES
                        ),
                        "deny": "0",
                    },
                    {
                        "id": role_id,
                        "type": 0,
                        "allow": str(VIEW_CHANNEL | READ_MESSAGE_HISTORY),
                        "deny": "0",
                    },
                ],
            },
        )
    else:
        put_overwrite(
            api,
            channel,
            GUILD_ID,
            allow_bits=VIEW_CHANNEL | READ_MESSAGE_HISTORY | ADD_REACTIONS,
            deny_bits=SEND_MESSAGES,
        )
        put_overwrite(
            api,
            channel,
            BOT_ROLE_ID,
            allow_bits=(
                VIEW_CHANNEL
                | SEND_MESSAGES
                | READ_MESSAGE_HISTORY
                | ADD_REACTIONS
                | MANAGE_MESSAGES
            ),
        )
    write_id(channel_id_file, str(channel["id"]))
    return channel


def ensure_verification_message(
    api: DiscordApi,
    channel_id: str,
    message_id_file: Path,
) -> str:
    saved_message_id = (
        message_id_file.read_text(encoding="ascii").strip()
        if message_id_file.exists()
        else ""
    )
    if saved_message_id:
        try:
            api.request("GET", f"/channels/{channel_id}/messages/{saved_message_id}")
            return saved_message_id
        except DiscordApiError:
            pass

    message = api.request(
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "embeds": [
                {
                    "title": "Property Case Radar｜新成員驗證",
                    "description": (
                        "歡迎加入伺服器。請先閱讀並同意以下規則：\n\n"
                        "1. 禁止垃圾訊息、廣告、詐騙及騷擾行為。\n"
                        "2. 不得利用法拍或房地資料從事違法行為。\n"
                        "3. 查詢結果僅供參考，投資與投標前須自行查證官方公告。\n"
                        "4. 請尊重其他成員並遵守 Discord 社群規範。\n\n"
                        "**驗證問題：你是否同意遵守以上規則？**\n"
                        "若同意，請點擊下方 ✅。系統會在 5 分鐘內授予 "
                        "`Radar 問答` 身分組並解鎖其他頻道。"
                    ),
                    "color": 3447003,
                    "footer": {"text": MESSAGE_MARKER},
                }
            ]
        },
    )
    message_id = str(message["id"])
    api.request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{quote('✅', safe='')}/@me",
    )
    write_id(message_id_file, message_id)
    return message_id


def lock_categories(
    api: DiscordApi,
    channels: list[dict[str, Any]],
    role_id: str,
) -> None:
    by_id = {str(channel["id"]): channel for channel in channels}
    for category_id in LOCKED_CATEGORY_IDS:
        category = by_id[category_id]
        # Grant the bot and verified role first. Denying @everyone first would
        # make the bot lose access to the category before it can finish the
        # remaining permission overwrites.
        put_overwrite(
            api,
            category,
            BOT_ROLE_ID,
            allow_bits=VIEW_CHANNEL | SEND_MESSAGES,
        )
        put_overwrite(
            api,
            category,
            role_id,
            allow_bits=VIEW_CHANNEL,
        )
        put_overwrite(
            api,
            category,
            GUILD_ID,
            deny_bits=VIEW_CHANNEL,
        )


def lock_child_channels(
    api: DiscordApi,
    channels: list[dict[str, Any]],
    role_id: str,
    verification_channel_id: str,
) -> int:
    locked = 0
    for channel in channels:
        if channel.get("type") not in {0, 2}:
            continue
        if str(channel["id"]) == verification_channel_id:
            continue
        bot_allow = VIEW_CHANNEL | (SEND_MESSAGES if channel.get("type") == 0 else 0)
        put_overwrite(
            api,
            channel,
            BOT_ROLE_ID,
            allow_bits=bot_allow,
        )
        put_overwrite(
            api,
            channel,
            role_id,
            allow_bits=VIEW_CHANNEL,
        )
        put_overwrite(
            api,
            channel,
            GUILD_ID,
            deny_bits=VIEW_CHANNEL,
        )
        locked += 1
    return locked


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--role-id-file", type=Path, required=True)
    parser.add_argument("--channel-id-file", type=Path, required=True)
    parser.add_argument("--message-id-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    api = DiscordApi(token)
    roles = api.request("GET", f"/guilds/{GUILD_ID}/roles")
    channels = api.request("GET", f"/guilds/{GUILD_ID}/channels")
    saved_role_id = args.role_id_file.read_text(encoding="ascii").strip()
    role_id = find_role_id(roles, saved_role_id)
    channel = ensure_verification_channel(api, channels, role_id, args.channel_id_file)
    message_id = ensure_verification_message(
        api,
        str(channel["id"]),
        args.message_id_file,
    )
    channels = api.request("GET", f"/guilds/{GUILD_ID}/channels")
    lock_categories(api, channels, role_id)
    channels = api.request("GET", f"/guilds/{GUILD_ID}/channels")
    locked_channel_count = lock_child_channels(
        api,
        channels,
        role_id,
        str(channel["id"]),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "role_id": role_id,
                "channel_id": str(channel["id"]),
                "message_id": message_id,
                "locked_category_count": len(LOCKED_CATEGORY_IDS),
                "locked_channel_count": locked_channel_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
