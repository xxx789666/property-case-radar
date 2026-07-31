from dataclasses import dataclass
from typing import Callable, Protocol

import discord

from database.models.rental import RentalProperty


class RentalMessageChannel(Protocol):
    async def send(self, *, embed: discord.Embed, content: str | None = None) -> object: ...


@dataclass(frozen=True)
class RentalNotification:
    title: str
    description: str
    url: str


def build_rental_notification(item: RentalProperty, kind: str) -> RentalNotification:
    title = {
        "price_drop": "📉 租屋降價",
        "high_score": "⭐ 高分租屋物件",
    }.get(kind, "🏠 新增租屋")
    median = (
        f"{item.district_median_rent_per_ping_twd:,} 元／坪"
        if item.district_median_rent_per_ping_twd else "樣本不足"
    )
    discount = (
        f"{float(item.discount_rate) * 100:.1f}%"
        if item.discount_rate is not None else "無法計算"
    )
    description = "\n".join(
        [
            f"標題：{item.title}",
            f"區域：{item.city}{item.district}",
            f"月租：{item.monthly_rent_twd:,} 元",
            f"坪數：{item.area_ping} 坪",
            f"租金單價：{item.rent_per_ping_twd:,} 元／坪",
            f"同區刊登中位數：{median}",
            f"低於同區：{discount}",
            f"類型：{item.rental_type or '資料未提供'}",
            f"格局：{item.layout or '資料未提供'}",
            f"特色：{item.features or '資料未提供'}",
            f"評分：{item.score if item.score is not None else '未評分'}／100",
            f"來源：{item.source}",
        ]
    )
    return RentalNotification(title=title, description=description, url=item.url)


class RentalNotificationRouter:
    def __init__(
        self,
        *,
        new_channel: RentalMessageChannel,
        price_drop_channel: RentalMessageChannel,
        high_score_channel: RentalMessageChannel,
        high_score_threshold: int = 80,
        channel_factory: Callable[[int], RentalMessageChannel] | None = None,
    ) -> None:
        self.new_channel = new_channel
        self.price_drop_channel = price_drop_channel
        self.high_score_channel = high_score_channel
        self.high_score_threshold = high_score_threshold
        self.channel_factory = channel_factory

    def subscription_channel(self, channel_id: int) -> RentalMessageChannel:
        if self.channel_factory is None:
            raise RuntimeError("subscription channel routing is not configured")
        return self.channel_factory(channel_id)

    async def publish(
        self,
        item: RentalProperty,
        *,
        created: bool,
        price_dropped: bool,
        send_new: bool,
        send_high_score: bool = True,
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
            send_high_score
            and (genuine_new or price_dropped)
            and item.score is not None
            and item.score >= self.high_score_threshold
        ):
            await self._send(self.high_score_channel, item, "high_score")
            kinds.append("high_score")
        return kinds

    @staticmethod
    async def _send(channel: RentalMessageChannel, item: RentalProperty, kind: str) -> None:
        note = build_rental_notification(item, kind)
        await channel.send(
            embed=discord.Embed(
                title=note.title,
                description=note.description,
                url=note.url,
                color=discord.Color.blue(),
            )
        )
