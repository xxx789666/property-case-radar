from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

from apps.discord_bot.cog import HouseCog


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()


@pytest.mark.asyncio
async def test_wrong_channel_is_rejected() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    cog = HouseCog(bot, Mock(), search_channel_id=123)
    interaction = SimpleNamespace(channel_id=999, response=FakeResponse())
    assert await cog._guard(interaction) is False
    interaction.response.send_message.assert_awaited_once_with(
        "請在「房地案件-搜尋」私人頻道使用此指令。", ephemeral=True
    )


def test_house_group_contains_all_required_commands() -> None:
    assert {command.name for command in HouseCog.house.commands} == {
        "search",
        "subscribe",
        "latest",
        "detail",
        "compare",
        "unsubscribe",
    }
