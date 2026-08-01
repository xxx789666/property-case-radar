from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

from apps.discord_bot.auction_cog import AuctionCog


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()


@pytest.mark.asyncio
async def test_wrong_channel_is_rejected() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    cog = AuctionCog(bot, Mock(), search_channel_id=123)
    interaction = SimpleNamespace(channel_id=999, response=FakeResponse())
    assert await cog._guard(interaction) is False
    interaction.response.send_message.assert_awaited_once_with(
        "請在「法拍案件-搜尋」私人頻道使用此指令。", ephemeral=True
    )


@pytest.mark.asyncio
async def test_correct_channel_passes_guard() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    cog = AuctionCog(bot, Mock(), search_channel_id=123)
    interaction = SimpleNamespace(channel_id=123, response=FakeResponse())
    assert await cog._guard(interaction) is True
    interaction.response.send_message.assert_not_awaited()


def test_auction_group_contains_all_required_commands() -> None:
    assert {command.name for command in AuctionCog.auction.commands} == {
        "search",
        "subscribe",
        "latest",
        "detail",
        "schedule",
        "risk",
        "unsubscribe",
    }


@pytest.mark.asyncio
async def test_detail_command_passes_private_audience_after_guard() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    service = Mock()
    service.detail.return_value = SimpleNamespace(text="ok", audience="private")
    cog = AuctionCog(bot, service, search_channel_id=123)
    interaction = SimpleNamespace(channel_id=123, response=FakeResponse())

    await cog.detail.callback(cog, interaction, "法院", "案號")

    service.detail.assert_called_once_with("法院", "案號", audience="private")
    interaction.response.send_message.assert_awaited_once_with("ok", ephemeral=True)


@pytest.mark.asyncio
async def test_detail_command_never_reaches_service_when_guard_fails() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    service = Mock()
    cog = AuctionCog(bot, service, search_channel_id=123)
    interaction = SimpleNamespace(channel_id=999, response=FakeResponse())

    await cog.detail.callback(cog, interaction, "法院", "案號")

    service.detail.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_uses_interaction_user_id() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    service = Mock()
    service.unsubscribe.return_value = SimpleNamespace(text="done", audience="private")
    cog = AuctionCog(bot, service, search_channel_id=123)
    interaction = SimpleNamespace(channel_id=123, response=FakeResponse(), user=SimpleNamespace(id=999))

    await cog.unsubscribe.callback(cog, interaction, 5)

    service.unsubscribe.assert_called_once_with(999, 5)
