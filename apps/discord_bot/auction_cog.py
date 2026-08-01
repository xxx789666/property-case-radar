"""``/auction search|subscribe|latest|detail|schedule|risk|unsubscribe``.

Mirrors ``apps.discord_bot.cog.HouseCog``'s shape: an
``app_commands.Group`` scoped to the private 法拍案件-搜尋 channel (per
discord 伺服器.txt), delegating all business logic to
``AuctionCommandService``. Every command explicitly passes
``audience="private"`` -- but only *after* ``_guard`` has confirmed the
interaction is actually in that private channel; the service layer's own
default stays "public" so a bug in (or bypass of) this guard fails safe
rather than leaking 債務人/所有權人 into a public channel.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.auction_service import AuctionCommandService, AuctionSearchInput
from database.models.auction import CaseType

_CASE_TYPE_CHOICES = [
    app_commands.Choice(name="住宅", value=CaseType.RESIDENTIAL.value),
    app_commands.Choice(name="店面", value=CaseType.STOREFRONT.value),
    app_commands.Choice(name="土地", value=CaseType.LAND.value),
    app_commands.Choice(name="廠辦", value=CaseType.OFFICE_FACTORY.value),
    app_commands.Choice(name="其他", value=CaseType.OTHER.value),
]


class AuctionCog(commands.Cog):
    auction = app_commands.Group(name="auction", description="法拍屋案件搜尋、訂閱與風險查詢")

    def __init__(self, bot: commands.Bot, service: AuctionCommandService, search_channel_id: int):
        self.bot = bot
        self.service = service
        self.search_channel_id = search_channel_id

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != self.search_channel_id:
            await interaction.response.send_message("請在「法拍案件-搜尋」私人頻道使用此指令。", ephemeral=True)
            return False
        return True

    @auction.command(name="search", description="依條件搜尋法拍案件")
    @app_commands.choices(case_type=_CASE_TYPE_CHOICES)
    async def search(
        self,
        interaction: discord.Interaction,
        city: str | None = None,
        district: str | None = None,
        case_type: app_commands.Choice[str] | None = None,
        max_floor_price_wan: int | None = None,
        min_round: int | None = None,
        require_deliverable: bool | None = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        values = AuctionSearchInput(
            city=city,
            district=district,
            case_type=CaseType(case_type.value) if case_type else None,
            max_floor_price_wan=max_floor_price_wan,
            min_round=min_round,
            require_deliverable=require_deliverable,
        )
        response = self.service.search(values, audience="private")
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="subscribe", description="訂閱法拍案件條件")
    @app_commands.choices(case_type=_CASE_TYPE_CHOICES)
    async def subscribe(
        self,
        interaction: discord.Interaction,
        city: str | None = None,
        district: str | None = None,
        case_type: app_commands.Choice[str] | None = None,
        max_floor_price_wan: int | None = None,
        min_round: int | None = None,
        require_deliverable: bool | None = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        values = AuctionSearchInput(
            city=city,
            district=district,
            case_type=CaseType(case_type.value) if case_type else None,
            max_floor_price_wan=max_floor_price_wan,
            min_round=min_round,
            require_deliverable=require_deliverable,
        )
        response = self.service.subscribe(interaction.user.id, values)
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="latest", description="查看最新法拍案件")
    async def latest(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5) -> None:
        if not await self._guard(interaction):
            return
        response = self.service.latest(limit=limit, audience="private")
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="detail", description="查看法拍案件詳細資料")
    async def detail(self, interaction: discord.Interaction, court_name: str, case_number: str) -> None:
        if not await self._guard(interaction):
            return
        response = self.service.detail(court_name, case_number, audience="private")
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="schedule", description="查看即將開標的法拍案件")
    async def schedule(self, interaction: discord.Interaction, within_days: app_commands.Range[int, 1, 30] = 7) -> None:
        if not await self._guard(interaction):
            return
        response = self.service.schedule(within_days=within_days, audience="private")
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="risk", description="查看法拍案件風險評估")
    async def risk(self, interaction: discord.Interaction, court_name: str, case_number: str) -> None:
        if not await self._guard(interaction):
            return
        response = self.service.risk(court_name, case_number, audience="private")
        await interaction.response.send_message(response.text, ephemeral=True)

    @auction.command(name="unsubscribe", description="取消法拍案件訂閱")
    async def unsubscribe(self, interaction: discord.Interaction, subscription_id: int) -> None:
        if not await self._guard(interaction):
            return
        response = self.service.unsubscribe(interaction.user.id, subscription_id)
        await interaction.response.send_message(response.text, ephemeral=True)
