from decimal import Decimal

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.service import HouseCommandService, HouseSearchInput
from notifications.sale_notification import build_sale_notification


def _embed(item: object) -> discord.Embed:
    note = build_sale_notification(item)
    return discord.Embed(
        title=note.title,
        description=note.description,
        url=note.url,
        color=discord.Color.green(),
    )


class HouseCog(commands.Cog):
    house = app_commands.Group(name="house", description="一般出售物件搜尋與訂閱")

    def __init__(self, bot: commands.Bot, service: HouseCommandService, search_channel_id: int):
        self.bot = bot
        self.service = service
        self.search_channel_id = search_channel_id

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != self.search_channel_id:
            await interaction.response.send_message("請在「房地案件-搜尋」私人頻道使用此指令。", ephemeral=True)
            return False
        return True

    @house.command(name="search", description="依條件搜尋出售物件")
    async def search(
        self,
        interaction: discord.Interaction,
        city: str,
        district: str | None = None,
        max_price_wan: int | None = None,
        min_area_ping: float | None = None,
        max_age_years: float | None = None,
        min_discount_percent: float | None = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        values = HouseSearchInput(
            city=city,
            district=district,
            max_price_wan=max_price_wan,
            min_area_ping=Decimal(str(min_area_ping)) if min_area_ping is not None else None,
            max_age_years=Decimal(str(max_age_years)) if max_age_years is not None else None,
            min_discount_percent=(
                Decimal(str(min_discount_percent)) if min_discount_percent is not None else None
            ),
        )
        items = self.service.search(values)
        if not items:
            await interaction.response.send_message("目前沒有符合條件的出售物件。", ephemeral=True)
            return
        await interaction.response.send_message(embeds=[_embed(item) for item in items], ephemeral=True)

    @house.command(name="subscribe", description="訂閱出售物件條件")
    async def subscribe(
        self,
        interaction: discord.Interaction,
        city: str,
        district: str | None = None,
        max_price_wan: int | None = None,
        min_area_ping: float | None = None,
        max_age_years: float | None = None,
        min_discount_percent: float | None = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        item = self.service.subscribe(
            interaction.user.id,
            HouseSearchInput(
                city=city,
                district=district,
                max_price_wan=max_price_wan,
                min_area_ping=Decimal(str(min_area_ping)) if min_area_ping is not None else None,
                max_age_years=Decimal(str(max_age_years)) if max_age_years is not None else None,
                min_discount_percent=(
                    Decimal(str(min_discount_percent)) if min_discount_percent is not None else None
                ),
            ),
        )
        await interaction.response.send_message(f"已建立訂閱 #{item.id}。", ephemeral=True)

    @house.command(name="latest", description="查看最新出售物件")
    async def latest(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5) -> None:
        if not await self._guard(interaction):
            return
        items = self.service.latest(limit)
        if not items:
            await interaction.response.send_message("目前尚無出售物件。", ephemeral=True)
            return
        await interaction.response.send_message(embeds=[_embed(item) for item in items], ephemeral=True)

    @house.command(name="detail", description="查看出售物件詳細資料")
    async def detail(self, interaction: discord.Interaction, property_id: int) -> None:
        if not await self._guard(interaction):
            return
        item = self.service.detail(property_id)
        if item is None:
            await interaction.response.send_message("找不到指定物件。", ephemeral=True)
            return
        await interaction.response.send_message(embed=_embed(item), ephemeral=True)

    @house.command(name="compare", description="比較物件掛牌價與區域實價")
    async def compare(self, interaction: discord.Interaction, property_id: int) -> None:
        if not await self._guard(interaction):
            return
        comparison = self.service.compare(property_id)
        await interaction.response.send_message(comparison or "找不到指定物件。", ephemeral=True)

    @house.command(name="unsubscribe", description="取消出售物件訂閱")
    async def unsubscribe(self, interaction: discord.Interaction, subscription_id: int) -> None:
        if not await self._guard(interaction):
            return
        changed = self.service.unsubscribe(interaction.user.id, subscription_id)
        await interaction.response.send_message(
            "已取消訂閱。" if changed else "找不到可取消的訂閱。", ephemeral=True
        )
