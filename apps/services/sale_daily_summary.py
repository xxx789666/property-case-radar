"""Daily county/city aggregate for the Discord sale-new channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.services.auction_daily_summary import TAIWAN_REGIONS
from database.models.base import utcnow
from database.models.common import NotificationLog
from database.models.sale import Property
from notifications.sale_notification import MessageChannel

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class DailySaleSummaryResult:
    day: date
    total: int
    delivered: bool
    skipped_duplicate: bool = False
    error: str | None = None


def daily_sale_counts(session: Session, day: date) -> dict[str, int]:
    start = datetime.combine(day, time.min, tzinfo=TAIPEI)
    end = start + timedelta(days=1)
    rows = session.execute(
        select(Property.city, func.count(Property.id))
        .where(Property.first_seen_at >= start)
        .where(Property.first_seen_at < end)
        .group_by(Property.city)
    )
    counts = {region: 0 for region in TAIWAN_REGIONS}
    for city, count in rows:
        if not city:
            continue
        canonical = city.replace("台", "臺")
        counts[canonical] = counts.get(canonical, 0) + int(count)
    return counts


def build_daily_sale_summary_embed(
    day: date,
    counts: dict[str, int],
) -> discord.Embed:
    total = sum(counts.values())
    embed = discord.Embed(
        title=f"📊 今日各縣市新增售屋｜{day.isoformat()}",
        description="\n".join(
            f"{region}：{counts.get(region, 0)} 筆" for region in TAIWAN_REGIONS
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"今日合計：{total} 筆")
    return embed


async def deliver_daily_sale_summary(
    session: Session,
    channel: MessageChannel,
    channel_id: int,
    *,
    day: date | None = None,
) -> DailySaleSummaryResult:
    summary_day = day or datetime.now(TAIPEI).date()
    delivery_key = f"sale:daily-summary:{summary_day.isoformat()}"
    row = session.scalar(
        select(NotificationLog).where(NotificationLog.delivery_key == delivery_key)
    )
    if row is not None and row.status == "delivered":
        counts = daily_sale_counts(session, summary_day)
        return DailySaleSummaryResult(
            day=summary_day,
            total=sum(counts.values()),
            delivered=False,
            skipped_duplicate=True,
        )

    if row is None:
        row = NotificationLog(
            kind="sale_daily_summary",
            delivery_key=delivery_key,
            channel_id=channel_id,
        )
        session.add(row)
        session.flush()

    counts = daily_sale_counts(session, summary_day)
    try:
        await channel.send(embed=build_daily_sale_summary_embed(summary_day, counts))
    except Exception as exc:  # noqa: BLE001 - persist failure for next retry
        row.status = "failed"
        row.attempt_count += 1
        row.last_error = str(exc)[:2000]
        session.commit()
        return DailySaleSummaryResult(
            day=summary_day,
            total=sum(counts.values()),
            delivered=False,
            error=str(exc),
        )

    row.status = "delivered"
    row.attempt_count += 1
    row.last_error = None
    row.delivered_at = utcnow()
    session.commit()
    return DailySaleSummaryResult(
        day=summary_day,
        total=sum(counts.values()),
        delivered=True,
    )
