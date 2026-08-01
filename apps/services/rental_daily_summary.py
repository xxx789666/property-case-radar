"""Daily county/city aggregate for the Discord rental-new channel."""

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
from database.models.rental import RentalProperty
from notifications.rental_notification import RentalMessageChannel

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class DailyRentalSummaryResult:
    day: date
    total: int
    delivered: bool
    skipped_duplicate: bool = False
    error: str | None = None


def daily_rental_counts(session: Session, day: date) -> dict[str, int]:
    start = datetime.combine(day, time.min, tzinfo=TAIPEI)
    end = start + timedelta(days=1)
    rows = session.execute(
        select(RentalProperty.city, func.count(RentalProperty.id))
        .where(
            RentalProperty.is_backfill.is_(False),
            RentalProperty.first_seen_at >= start,
            RentalProperty.first_seen_at < end,
        )
        .group_by(RentalProperty.city)
    )
    counts = {region: 0 for region in TAIWAN_REGIONS}
    for city, count in rows:
        if city:
            canonical = city.replace("台", "臺")
            counts[canonical] = counts.get(canonical, 0) + int(count)
    return counts


def build_daily_rental_summary_embed(
    day: date,
    counts: dict[str, int],
) -> discord.Embed:
    total = sum(counts.values())
    embed = discord.Embed(
        title=f"今日各縣市新增租屋｜{day.isoformat()}",
        description="\n".join(
            f"{region}：{counts.get(region, 0)} 筆" for region in TAIWAN_REGIONS
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"今日合計：{total} 筆")
    return embed


async def deliver_daily_rental_summary(
    session: Session,
    channel: RentalMessageChannel,
    channel_id: int,
    *,
    day: date | None = None,
) -> DailyRentalSummaryResult:
    summary_day = day or datetime.now(TAIPEI).date()
    delivery_key = f"rental:daily-summary:{summary_day.isoformat()}"
    row = session.scalar(
        select(NotificationLog).where(NotificationLog.delivery_key == delivery_key)
    )
    if row is not None and row.status == "delivered":
        counts = daily_rental_counts(session, summary_day)
        return DailyRentalSummaryResult(
            day=summary_day,
            total=sum(counts.values()),
            delivered=False,
            skipped_duplicate=True,
        )
    if row is None:
        row = NotificationLog(
            kind="rental_daily_summary",
            delivery_key=delivery_key,
            channel_id=channel_id,
        )
        session.add(row)
        session.flush()

    counts = daily_rental_counts(session, summary_day)
    try:
        await channel.send(embed=build_daily_rental_summary_embed(summary_day, counts))
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.attempt_count += 1
        row.last_error = str(exc)[:2000]
        session.commit()
        return DailyRentalSummaryResult(
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
    return DailyRentalSummaryResult(
        day=summary_day,
        total=sum(counts.values()),
        delivered=True,
    )
