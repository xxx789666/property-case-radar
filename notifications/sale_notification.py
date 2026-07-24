from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import discord
from database.models.sale import Property


@dataclass(frozen=True)
class SaleNotification:
    title: str
    description: str
    url: str
    score: int | None


class MessageChannel(Protocol):
    async def send(self, *, embed: discord.Embed) -> object: ...


class SaleNotificationRouter:
    def __init__(
        self,
        *,
        new_channel: MessageChannel,
        price_drop_channel: MessageChannel,
        high_score_channel: MessageChannel,
        high_score_threshold: int = 80,
    ):
        self.new_channel = new_channel
        self.price_drop_channel = price_drop_channel
        self.high_score_channel = high_score_channel
        self.high_score_threshold = high_score_threshold

    async def publish(self, item: Property, *, created: bool, price_dropped: bool) -> list[str]:
        kinds: list[str] = []
        if created:
            await self._send(self.new_channel, item, "new")
            kinds.append("new")
        if price_dropped:
            await self._send(self.price_drop_channel, item, "price_drop")
            kinds.append("price_drop")
        if item.score is not None and item.score >= self.high_score_threshold:
            await self._send(self.high_score_channel, item, "high_score")
            kinds.append("high_score")
        return kinds

    @staticmethod
    async def _send(channel: MessageChannel, item: Property, kind: str) -> None:
        note = build_sale_notification(item, kind=kind)
        embed = discord.Embed(
            title=note.title,
            description=note.description,
            url=note.url,
            color=discord.Color.green(),
        )
        await channel.send(embed=embed)


def format_twd_wan(amount_twd: int) -> str:
    return f"{amount_twd / 10_000:,.0f} 萬"


def format_discount(rate: Decimal | None) -> str:
    return "尚無行情" if rate is None else f"{float(rate) * 100:.1f}%"


def build_sale_notification(item: Property, *, kind: str = "new") -> SaleNotification:
    prefixes = {"new": "🏠 新增出售案件", "price_drop": "📉 出售物件降價", "high_score": "⭐ 高分出售物件"}
    title = prefixes.get(kind, prefixes["new"])
    market = (
        f"{item.market_unit_price_twd / 10_000:,.1f} 萬／坪"
        if item.market_unit_price_twd
        else "尚無行情"
    )
    description = "\n".join(
        [
            f"區域：{item.city}{item.district}",
            f"總價：{format_twd_wan(item.total_price_twd)}",
            f"建坪：{item.building_area_ping} 坪",
            f"掛牌單價：{item.unit_price_per_ping_twd / 10_000:,.1f} 萬／坪",
            f"區域成交均價：{market}",
            f"低於行情：{format_discount(item.discount_rate)}",
            f"評分：{item.score if item.score is not None else '未評分'}／100",
            f"來源：{item.source}",
        ]
    )
    return SaleNotification(title=title, description=description, url=item.url, score=item.score)
