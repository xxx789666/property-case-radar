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
    failed_regions: tuple[str, ...] = ()
    message_id: int | None = None


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
    *,
    failed_regions: tuple[str, ...] = (),
) -> discord.Embed:
    total = sum(counts.values())
    failed = {region.replace("台", "臺") for region in failed_regions}
    embed = discord.Embed(
        title=f"今日各縣市新增租屋｜{day.isoformat()}",
        description="\n".join(
            (
                f"{region}：抓取失敗／待重試"
                if region in failed
                else f"{region}：{counts.get(region, 0)} 筆"
            )
            for region in TAIWAN_REGIONS
        ),
        color=discord.Color.green(),
    )
    footer = f"今日合計：{total} 筆"
    if failed:
        footer += f"｜待重試：{'、'.join(sorted(failed))}"
    embed.set_footer(text=footer)
    return embed


async def deliver_daily_rental_summary(
    session: Session,
    channel: RentalMessageChannel,
    channel_id: int,
    *,
    day: date | None = None,
    failed_regions: tuple[str, ...] = (),
) -> DailyRentalSummaryResult:
    summary_day = day or datetime.now(TAIPEI).date()
    delivery_key = f"rental:daily-summary:{summary_day.isoformat()}"
    row = session.scalar(
        select(NotificationLog).where(NotificationLog.delivery_key == delivery_key)
    )
    if row is not None and row.status == "delivered":
        counts = daily_rental_counts(session, summary_day)
        message_id = None
        refresh_error = None
        finder = getattr(channel, "find_message_id", None)
        if callable(finder):
            try:
                refreshed_embed = build_daily_rental_summary_embed(
                    summary_day,
                    counts,
                    failed_regions=failed_regions,
                )
                message_id = await finder(embed_title=refreshed_embed.title)
                if message_id is not None:
                    await channel.edit(
                        message_id=int(message_id),
                        embed=refreshed_embed,
                    )
            except Exception as exc:  # noqa: BLE001 - retain idempotency
                refresh_error = str(exc)
        return DailyRentalSummaryResult(
            day=summary_day,
            total=sum(counts.values()),
            delivered=False,
            skipped_duplicate=True,
            error=refresh_error,
            failed_regions=failed_regions,
            message_id=int(message_id) if message_id is not None else None,
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
        message = await channel.send(
            embed=build_daily_rental_summary_embed(
                summary_day,
                counts,
                failed_regions=failed_regions,
            )
        )
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
            failed_regions=failed_regions,
        )

    row.status = "delivered"
    row.attempt_count += 1
    row.last_error = None
    row.delivered_at = utcnow()
    session.commit()
    message_id = getattr(message, "id", None)
    return DailyRentalSummaryResult(
        day=summary_day,
        total=sum(counts.values()),
        delivered=True,
        failed_regions=failed_regions,
        message_id=int(message_id) if message_id is not None else None,
    )


async def update_daily_rental_summary(
    session: Session,
    channel: RentalMessageChannel,
    message_id: int,
    *,
    day: date,
    failed_regions: tuple[str, ...] = (),
) -> DailyRentalSummaryResult:
    """Edit an already-delivered summary after failed-city recapture."""

    counts = daily_rental_counts(session, day)
    try:
        await channel.edit(
            message_id=message_id,
            embed=build_daily_rental_summary_embed(
                day,
                counts,
                failed_regions=failed_regions,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - retry cycle must continue
        return DailyRentalSummaryResult(
            day=day,
            total=sum(counts.values()),
            delivered=False,
            error=str(exc),
            failed_regions=failed_regions,
            message_id=message_id,
        )
    return DailyRentalSummaryResult(
        day=day,
        total=sum(counts.values()),
        delivered=True,
        failed_regions=failed_regions,
        message_id=message_id,
    )
