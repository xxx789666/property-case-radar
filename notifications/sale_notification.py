from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

import discord
from database.models.sale import Property


@dataclass(frozen=True)
class SaleNotification:
    title: str
    description: str
    url: str
    score: int | None


class MessageChannel(Protocol):
    async def send(
        self, *, embed: discord.Embed, content: str | None = None
    ) -> object: ...


class SaleNotificationRouter:
    def __init__(
        self,
        *,
        new_channel: MessageChannel,
        price_drop_channel: MessageChannel,
        high_score_channel: MessageChannel,
        high_score_threshold: int = 80,
        channel_factory: Callable[[int], MessageChannel] | None = None,
    ):
        self.new_channel = new_channel
        self.price_drop_channel = price_drop_channel
        self.high_score_channel = high_score_channel
        self.high_score_threshold = high_score_threshold
        self.channel_factory = channel_factory

    def subscription_channel(self, channel_id: int) -> MessageChannel:
        if self.channel_factory is None:
            raise RuntimeError("subscription channel routing is not configured")
        return self.channel_factory(channel_id)

    async def publish(
        self,
        item: Property,
        *,
        created: bool,
        price_dropped: bool,
        send_new: bool = True,
    ) -> list[str]:
        kinds: list[str] = []
        genuine_new = created and not item.is_backfill
        if genuine_new and send_new:
            await self._send(self.new_channel, item, "new")
            kinds.append("new")
        if price_dropped:
            await self._send(self.price_drop_channel, item, "price_drop")
            kinds.append("price_drop")
        if (
            (genuine_new or price_dropped)
            and item.score is not None
            and item.score >= self.high_score_threshold
        ):
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
    area_label = "土地坪數" if item.building_type == "土地" else "建坪"
    area_value = (
        item.land_area_ping
        if item.building_type == "土地" and item.land_area_ping is not None
        else item.building_area_ping
    )
    lines = [
        f"區域：{item.city}{item.district}",
        f"總價：{format_twd_wan(item.total_price_twd)}",
    ]
    if item.building_type == "土地":
        lines.append(f"土地類型：{item.usage or '資料未提供'}")
    lines.extend(
        [
            f"{area_label}：{area_value} 坪",
            f"掛牌單價：{item.unit_price_per_ping_twd / 10_000:,.1f} 萬／坪",
            f"區域成交均價：{market}",
            f"低於行情：{format_discount(item.discount_rate)}",
            f"評分：{item.score if item.score is not None else '未評分'}／100",
            f"來源：{item.source}",
        ]
    )
    description = "\n".join(lines)
    return SaleNotification(title=title, description=description, url=item.url, score=item.score)
