from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.base import utcnow
from database.models.common import NotificationLog
from database.models.rental import RentalProperty
from database.models.sale import Property

SALE_KIND = "sale_high_score_digest"
RENTAL_KIND = "rental_high_score_digest"
MAX_NOTIFY_ATTEMPTS = 5


class DigestChannel(Protocol):
    async def send(
        self,
        *,
        embed: discord.Embed,
        content: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class HighScoreDigestReport:
    queued: int = 0
    attempted: int = 0
    delivered: int = 0
    suppressed: int = 0
    failed: int = 0
    discord_messages: int = 0


def _already_queued(session: Session, delivery_key: str) -> bool:
    return (
        session.scalar(
            select(NotificationLog.id).where(
                NotificationLog.delivery_key == delivery_key
            )
        )
        is not None
    )


def queue_sale_high_score(
    session: Session,
    item: Property,
    *,
    created: bool,
    price_dropped: bool,
    threshold: int = 80,
) -> int:
    if (
        item.is_backfill
        or not (created or price_dropped)
        or item.score is None
        or item.score < threshold
    ):
        return 0
    session.flush()
    delivery_key = f"sale:high-score:{item.id}:price:{item.total_price_twd}"
    if _already_queued(session, delivery_key):
        return 0
    session.add(
        NotificationLog(
            property_id=item.id,
            kind=SALE_KIND,
            delivery_key=delivery_key,
        )
    )
    return 1


def queue_rental_high_score(
    session: Session,
    item: RentalProperty,
    *,
    created: bool,
    price_dropped: bool,
    threshold: int = 80,
) -> int:
    if (
        item.is_backfill
        or not (created or price_dropped)
        or item.score is None
        or item.score < threshold
    ):
        return 0
    session.flush()
    delivery_key = (
        f"rental:high-score:{item.id}:rent:{item.monthly_rent_twd}"
    )
    if _already_queued(session, delivery_key):
        return 0
    session.add(
        NotificationLog(
            rental_property_id=item.id,
            kind=RENTAL_KIND,
            delivery_key=delivery_key,
        )
    )
    return 1


def _sale_line(item: Property) -> str:
    title = (item.address or item.building_type or "未命名物件").replace(
        "\n", " "
    )[:42]
    price = f"{item.total_price_twd / 10_000:,.0f} 萬"
    return (
        f"**{item.score} 分**｜{item.city}{item.district}｜{price}｜"
        f"[{title}]({item.url})"
    )


def _rental_line(item: RentalProperty) -> str:
    title = (item.title or item.address or "未命名物件").replace("\n", " ")[:42]
    return (
        f"**{item.score} 分**｜{item.city}{item.district}｜"
        f"{item.monthly_rent_twd:,} 元／月｜[{title}]({item.url})"
    )


def _sort_key(item) -> tuple[int, Decimal, int]:
    return (
        item.score if item.score is not None else -1,
        item.discount_rate
        if item.discount_rate is not None
        else Decimal("-999"),
        item.id,
    )


def _build_embed(
    *,
    source: str,
    items: list[Property] | list[RentalProperty],
    total: int,
    limit: int,
    day: date,
) -> discord.Embed:
    is_sale = source == "sale"
    label = "售屋" if is_sale else "租屋"
    lines = [
        _sale_line(item) if is_sale else _rental_line(item)
        for item in items
    ]
    hidden = max(0, total - len(items))
    footer = (
        f"\n\n其餘 {hidden} 筆請在案件搜尋頻道詢問 "
        f"`@Property Case Radar 幫我查詢高分{label}案件`。"
        if hidden
        else ""
    )
    description = (
        f"本批符合 80 分以上共 **{total} 筆**，以下列出最高分前 "
        f"**{min(limit, total)} 筆**。\n\n"
        + "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))
        + footer
    )
    return discord.Embed(
        title=f"⭐ 每日高分{label}摘要｜{day.isoformat()}",
        description=description[:4096],
        color=discord.Color.gold(),
    )


async def deliver_high_score_digest(
    session: Session,
    channel: DigestChannel,
    *,
    source: str,
    limit: int = 20,
    max_attempts: int = MAX_NOTIFY_ATTEMPTS,
    day: date | None = None,
) -> HighScoreDigestReport:
    if source not in {"sale", "rental"}:
        raise ValueError("source must be 'sale' or 'rental'")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    kind = SALE_KIND if source == "sale" else RENTAL_KIND
    rows = list(
        session.scalars(
            select(NotificationLog)
            .where(NotificationLog.kind == kind)
            .where(NotificationLog.status.in_(("pending", "failed")))
            .where(NotificationLog.attempt_count < max_attempts)
            .order_by(NotificationLog.id)
        )
    )
    if not rows:
        return HighScoreDigestReport()

    resolved = []
    for row in rows:
        model = Property if source == "sale" else RentalProperty
        item_id = row.property_id if source == "sale" else row.rental_property_id
        item = session.get(model, item_id)
        if item is None:
            row.status = "failed"
            row.attempt_count += 1
            row.last_error = "referenced property no longer exists"
            continue
        resolved.append((row, item))
    session.commit()
    if not resolved:
        return HighScoreDigestReport(
            attempted=len(rows),
            failed=len(rows),
        )

    resolved.sort(key=lambda pair: _sort_key(pair[1]), reverse=True)
    selected = resolved[:limit]
    remainder = resolved[limit:]
    embed = _build_embed(
        source=source,
        items=[item for _, item in selected],
        total=len(resolved),
        limit=limit,
        day=day or date.today(),
    )
    channel_id = getattr(channel, "channel_id", None)
    try:
        await channel.send(embed=embed)
    except Exception as error:
        diagnostic = str(error)[:2000]
        for row, _ in resolved:
            row.status = "failed"
            row.attempt_count += 1
            row.last_error = diagnostic
        session.commit()
        return HighScoreDigestReport(
            attempted=len(resolved),
            failed=len(resolved),
        )

    delivered_at = utcnow()
    for row, _ in selected:
        row.status = "delivered"
        row.channel_id = channel_id
        row.attempt_count += 1
        row.last_error = None
        row.delivered_at = delivered_at
    for row, _ in remainder:
        row.status = "suppressed"
        row.channel_id = channel_id
        row.attempt_count += 1
        row.last_error = f"not included in top {limit} daily digest"
    session.commit()
    return HighScoreDigestReport(
        attempted=len(resolved),
        delivered=len(selected),
        suppressed=len(remainder),
        discord_messages=1,
    )
