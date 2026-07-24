"""Coverage for apps.services.auction_notifier.live_auction_notifier --
Fix (1): production scheduler/bot composition must build a real,
working AuctionNotificationRouter when a Discord token is configured
rather than hardcoding notifier=None, must never surface the token in
logs, must explicitly disable (not crash) on a missing/invalid token,
and must always release its HTTP session on the way out.

No live Discord connection anywhere in this file: discord.Client.login
and discord.Client.close are always patched with AsyncMock.
"""

import logging
from unittest.mock import AsyncMock, patch

import discord
import pytest

from apps.config import Settings
from apps.services.auction_notifier import DiscordRestChannelSender, live_auction_notifier
from notifications.auction_notification import AuctionNotificationRouter

_FAKE_TOKEN = "fake-token-for-tests-do-not-use.abcdef.ghijklmnop"  # nosec: not a real credential


@pytest.mark.asyncio
async def test_missing_token_yields_none_and_logs_a_clear_warning(caplog) -> None:
    settings = Settings(discord_token=None)

    with caplog.at_level(logging.WARNING):
        async with live_auction_notifier(settings) as notifier:
            assert notifier is None

    assert any("DISCORD_TOKEN is not set" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_login_failure_disables_notifications_and_never_logs_the_token(caplog) -> None:
    settings = Settings(discord_token=_FAKE_TOKEN)

    with (
        patch.object(discord.Client, "login", AsyncMock(side_effect=discord.LoginFailure("invalid token"))),
        patch.object(discord.Client, "close", AsyncMock()) as close_mock,
        caplog.at_level(logging.WARNING),
    ):
        async with live_auction_notifier(settings) as notifier:
            assert notifier is None

    close_mock.assert_awaited_once()
    assert any("Discord login failed" in record.message for record in caplog.records)
    full_log_text = "\n".join(record.message for record in caplog.records)
    assert _FAKE_TOKEN not in full_log_text


@pytest.mark.asyncio
async def test_unexpected_login_error_disables_notifications_and_never_logs_the_token(caplog) -> None:
    settings = Settings(discord_token=_FAKE_TOKEN)

    with (
        patch.object(discord.Client, "login", AsyncMock(side_effect=RuntimeError("network unreachable"))),
        patch.object(discord.Client, "close", AsyncMock()) as close_mock,
        caplog.at_level(logging.WARNING),
    ):
        async with live_auction_notifier(settings) as notifier:
            assert notifier is None

    close_mock.assert_awaited_once()
    full_log_text = "\n".join(record.message for record in caplog.records)
    assert _FAKE_TOKEN not in full_log_text


@pytest.mark.asyncio
async def test_valid_token_yields_a_working_router_wired_to_all_five_channels() -> None:
    settings = Settings(discord_token=_FAKE_TOKEN)

    with (
        patch.object(discord.Client, "login", AsyncMock()) as login_mock,
        patch.object(discord.Client, "close", AsyncMock()) as close_mock,
    ):
        async with live_auction_notifier(settings) as notifier:
            assert isinstance(notifier, AuctionNotificationRouter)
            for attr in ("new_channel", "upcoming_channel", "round_channel", "high_score_channel", "suspended_channel"):
                assert isinstance(getattr(notifier, attr), DiscordRestChannelSender)
            # The router isn't closed yet while still inside the "with".
            close_mock.assert_not_awaited()

    login_mock.assert_awaited_once_with(_FAKE_TOKEN)
    close_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_is_closed_even_when_the_caller_body_raises() -> None:
    settings = Settings(discord_token=_FAKE_TOKEN)

    with (
        patch.object(discord.Client, "login", AsyncMock()),
        patch.object(discord.Client, "close", AsyncMock()) as close_mock,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            async with live_auction_notifier(settings):
                raise RuntimeError("boom")

    close_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_rest_channel_sender_fetches_the_channel_fresh_and_sends() -> None:
    client = AsyncMock()
    channel = AsyncMock()
    client.fetch_channel = AsyncMock(return_value=channel)
    sender = DiscordRestChannelSender(client, 123456789)

    embed = discord.Embed(title="test")
    await sender.send(embed=embed)

    client.fetch_channel.assert_awaited_once_with(123456789)
    channel.send.assert_awaited_once_with(embed=embed)
