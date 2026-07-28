"""Daily county/city aggregate for the Discord auction-new channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models.auction import AuctionCase, AuctionRound
from database.models.base import utcnow
from database.models.common import NotificationLog
from notifications.auction_notification import MessageChannel

TAIPEI = ZoneInfo("Asia/Taipei")
TAIWAN_REGIONS = (
    "臺北市",
    "新北市",
    "桃園市",
    "臺中市",
    "臺南市",
    "高雄市",
    "基隆市",
    "新竹市",
    "新竹縣",
    "苗栗縣",
    "彰化縣",
    "南投縣",
    "雲林縣",
    "嘉義市",
    "嘉義縣",
    "屏東縣",
    "宜蘭縣",
    "花蓮縣",
    "臺東縣",
    "澎湖縣",
    "金門縣",
    "連江縣",
)


@dataclass(frozen=True)
class DailySummaryResult:
    day: date
    total: int
    delivered: bool
    skipped_duplicate: bool = False
    error: str | None = None
    failed_regions: tuple[str, ...] = ()
    message_id: int | None = None


def daily_auction_counts(
    session: Session,
    day: date,
    *,
    round_number: int | None = None,
) -> dict[str, int]:
    start = datetime.combine(day, time.min, tzinfo=TAIPEI)
    end = start + timedelta(days=1)
    statement = (
        select(AuctionCase.city, func.count(func.distinct(AuctionCase.id)))
        .where(AuctionCase.first_seen_at >= start)
        .where(AuctionCase.first_seen_at < end)
        .group_by(AuctionCase.city)
    )
    if round_number is not None:
        latest_rounds = (
            select(
                AuctionRound.case_id.label("case_id"),
                func.max(AuctionRound.round_number).label("current_round_number"),
            )
            .group_by(AuctionRound.case_id)
            .subquery()
        )
        statement = (
            statement.join(latest_rounds, latest_rounds.c.case_id == AuctionCase.id)
            .where(latest_rounds.c.current_round_number == round_number)
        )
    rows = session.execute(statement)
    counts = {region: 0 for region in TAIWAN_REGIONS}
    for city, count in rows:
        if not city:
            continue
        canonical = city.replace("台", "臺")
        counts[canonical] = counts.get(canonical, 0) + int(count)
    return counts


def build_daily_summary_embed(
    day: date,
    counts: dict[str, int],
    *,
    round_number: int | None = None,
    failed_regions: tuple[str, ...] = (),
) -> discord.Embed:
    total = sum(counts.values())
    failed = {region.replace("台", "臺") for region in failed_regions}
    lines = [
        (
            f"{region}：抓取失敗／待重試"
            if region in failed
            else f"{region}：{counts.get(region, 0)} 筆"
        )
        for region in TAIWAN_REGIONS
    ]
    round_label = {2: "二拍", 3: "三拍"}.get(round_number, f"第{round_number}拍")
    subject = f"{round_label}新增法拍" if round_number is not None else "新增法拍"
    embed = discord.Embed(
        title=f"📊 今日各縣市{subject}｜{day.isoformat()}",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    footer = f"今日合計：{total} 筆"
    if failed:
        footer += f"｜抓取失敗：{'、'.join(sorted(failed))}"
    embed.set_footer(text=footer)
    return embed


async def deliver_daily_auction_summary(
    session: Session,
    channel: MessageChannel,
    channel_id: int,
    *,
    day: date | None = None,
    round_number: int | None = None,
    failed_regions: tuple[str, ...] = (),
) -> DailySummaryResult:
    summary_day = day or datetime.now(TAIPEI).date()
    delivery_key = (
        f"auction:daily-round-{round_number}-summary:{summary_day.isoformat()}"
        if round_number is not None
        else f"auction:daily-summary:{summary_day.isoformat()}"
    )
    row = session.scalar(
        select(NotificationLog).where(NotificationLog.delivery_key == delivery_key)
    )
    if row is not None and row.status == "delivered":
        counts = daily_auction_counts(session, summary_day, round_number=round_number)
        message_id = None
        finder = getattr(channel, "find_message_id", None)
        if callable(finder):
            try:
                title = build_daily_summary_embed(
                    summary_day,
                    counts,
                    round_number=round_number,
                    failed_regions=failed_regions,
                ).title
                message_id = await finder(embed_title=title)
            except Exception:
                message_id = None
        return DailySummaryResult(
            day=summary_day,
            total=sum(counts.values()),
            delivered=False,
            skipped_duplicate=True,
            failed_regions=failed_regions,
            message_id=int(message_id) if message_id is not None else None,
        )

    if row is None:
        row = NotificationLog(
            kind=(f"daily_round_{round_number}_summary" if round_number is not None else "daily_summary"),
            delivery_key=delivery_key,
            channel_id=channel_id,
        )
        session.add(row)
        session.flush()

    counts = daily_auction_counts(session, summary_day, round_number=round_number)
    try:
        message = await channel.send(
            embed=build_daily_summary_embed(
                summary_day,
                counts,
                round_number=round_number,
                failed_regions=failed_regions,
            )
        )
    except Exception as exc:  # noqa: BLE001 - persist failure for the next scheduler retry
        row.status = "failed"
        row.attempt_count += 1
        row.last_error = str(exc)[:2000]
        session.commit()
        return DailySummaryResult(
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
    return DailySummaryResult(
        day=summary_day,
        total=sum(counts.values()),
        delivered=True,
        failed_regions=failed_regions,
        message_id=int(message_id) if message_id is not None else None,
    )


async def update_daily_auction_summary(
    session: Session,
    channel: MessageChannel,
    message_id: int,
    *,
    day: date,
    round_number: int | None = None,
    failed_regions: tuple[str, ...] = (),
) -> DailySummaryResult:
    """Edit an already-delivered daily summary after a county retry."""

    counts = daily_auction_counts(session, day, round_number=round_number)
    try:
        await channel.edit(
            message_id=message_id,
            embed=build_daily_summary_embed(
                day,
                counts,
                round_number=round_number,
                failed_regions=failed_regions,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - retry cycle must continue
        return DailySummaryResult(
            day=day,
            total=sum(counts.values()),
            delivered=False,
            error=str(exc),
            failed_regions=failed_regions,
            message_id=message_id,
        )
    return DailySummaryResult(
        day=day,
        total=sum(counts.values()),
        delivered=True,
        failed_regions=failed_regions,
        message_id=message_id,
    )
