from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.auction import (
    ACTIVE_STATUSES,
    CASE_TYPE_LABELS,
    AuctionCase,
    AuctionSubscription,
)
from database.models.base import utcnow
from database.models.common import NotificationLog
from database.models.sale import Property, PropertySubscription
from notifications.sale_notification import build_sale_notification

MAX_ATTEMPTS = 5


class SubscriptionChannel(Protocol):
    async def send(
        self, *, embed: discord.Embed, content: str | None = None
    ) -> object: ...


def _sale_type_matches(item: Property, property_type: str | None) -> bool:
    if property_type is None:
        return True
    is_land = item.building_type == "土地"
    usage = item.usage or ""
    if property_type == "house":
        return not is_land
    if property_type == "land":
        return is_land
    markers = {
        "farmland": ("農地",),
        "building_land": (
            "建地",
            "建築用地",
            "建築基地",
            "住宅用地",
            "商業用地",
            "工業用地",
            "工業地",
            "住宅區",
            "商業區",
            "工業區",
            "甲建",
            "乙建",
            "丙建",
            "丁建",
            "特定目的事業用地",
        ),
        "residential_land": ("住宅用地", "住宅區"),
        "commercial_land": ("商業用地", "商業區"),
        "industrial_land": (
            "工業用地",
            "工業地",
            "工業區",
            "丁種建築用地",
            "丁種建築",
            "丁建",
        ),
        "type_a_building_land": ("甲種建築用地", "甲種建築", "甲建"),
        "type_b_building_land": ("乙種建築用地", "乙種建築", "乙建"),
        "type_c_building_land": ("丙種建築用地", "丙種建築", "丙建"),
        "type_d_building_land": ("丁種建築用地", "丁種建築", "丁建"),
        "forest_land": ("林地",),
        "hillside_land": ("山坡地",),
        "road_land": ("道路用地",),
    }
    return is_land and any(marker in usage for marker in markers.get(property_type, ()))


def _sale_matches(item: Property, sub: PropertySubscription) -> bool:
    return (
        item.status == "active"
        and item.city == sub.city
        and (sub.district is None or item.district == sub.district)
        and (
            sub.max_total_price_twd is None
            or item.total_price_twd <= sub.max_total_price_twd
        )
        and (
            sub.min_building_area_ping is None
            or item.building_area_ping >= sub.min_building_area_ping
        )
        and (
            sub.max_age_years is None
            or (item.age_years is not None and item.age_years <= sub.max_age_years)
        )
        and (
            sub.min_discount_rate is None
            or (
                item.discount_rate is not None
                and item.discount_rate >= sub.min_discount_rate
            )
        )
        and _sale_type_matches(item, sub.property_type)
    )


def _auction_matches(case: AuctionCase, sub: AuctionSubscription) -> bool:
    current = case.current_round
    return (
        case.status in ACTIVE_STATUSES
        and (sub.city is None or case.city == sub.city)
        and (sub.district is None or case.district == sub.district)
        and (sub.case_type is None or case.case_type == sub.case_type)
        and (
            sub.max_floor_price_twd is None
            or (
                current is not None
                and current.floor_price_total_twd <= sub.max_floor_price_twd
            )
        )
        and (
            sub.min_round is None
            or (current is not None and current.round_number >= sub.min_round)
        )
        and (
            sub.require_deliverable is None
            or case.is_deliverable == sub.require_deliverable
        )
        and (
            sub.min_investment_score is None
            or (
                case.investment_score is not None
                and case.investment_score >= sub.min_investment_score
            )
        )
    )


def _queue(
    session: Session,
    *,
    kind: str,
    subscription_id: int,
    channel_id: int,
    property_id: int | None = None,
    auction_case_id: int | None = None,
) -> int:
    object_id = property_id if property_id is not None else auction_case_id
    key = f"subscription:{kind}:{subscription_id}:{object_id}"
    if session.scalar(
        select(NotificationLog.id).where(NotificationLog.delivery_key == key)
    ) is not None:
        return 0
    session.add(
        NotificationLog(
            property_id=property_id,
            auction_case_id=auction_case_id,
            channel_id=channel_id,
            kind=f"subscription:{kind}:{subscription_id}",
            delivery_key=key,
        )
    )
    return 1


def queue_sale_subscription_matches(session: Session, item: Property) -> int:
    session.flush()
    subscriptions = session.scalars(
        select(PropertySubscription).where(PropertySubscription.active.is_(True))
    ).all()
    return sum(
        _queue(
            session,
            kind="sale",
            subscription_id=sub.id,
            channel_id=sub.channel_id,
            property_id=item.id,
        )
        for sub in subscriptions
        if sub.channel_id is not None and _sale_matches(item, sub)
    )


def queue_auction_subscription_matches(session: Session, case: AuctionCase) -> int:
    session.flush()
    subscriptions = session.scalars(
        select(AuctionSubscription).where(AuctionSubscription.active.is_(True))
    ).all()
    return sum(
        _queue(
            session,
            kind="auction",
            subscription_id=sub.id,
            channel_id=sub.channel_id,
            auction_case_id=case.id,
        )
        for sub in subscriptions
        if sub.channel_id is not None and _auction_matches(case, sub)
    )


@dataclass(frozen=True)
class SubscriptionDeliveryReport:
    attempted: int = 0
    delivered: int = 0
    failed: int = 0


async def deliver_pending_subscription_notifications(
    session: Session,
    channel_factory: Callable[[int], SubscriptionChannel],
) -> SubscriptionDeliveryReport:
    rows = session.scalars(
        select(NotificationLog)
        .where(NotificationLog.kind.like("subscription:%"))
        .where(NotificationLog.status.in_(("pending", "failed")))
        .where(NotificationLog.attempt_count < MAX_ATTEMPTS)
        .order_by(NotificationLog.id)
    ).all()
    attempted = delivered = failed = 0
    for row in rows:
        attempted += 1
        match = re.fullmatch(r"subscription:(sale|auction):(\d+)", row.kind)
        if match is None or row.channel_id is None:
            row.status = "failed"
            row.last_error = "invalid subscription outbox row"
            row.attempt_count += 1
            failed += 1
            session.commit()
            continue
        kind, raw_id = match.groups()
        subscription_id = int(raw_id)
        if kind == "sale":
            subscription = session.get(PropertySubscription, subscription_id)
            item = session.get(Property, row.property_id)
            active = subscription is not None and subscription.active and item is not None
            if active:
                note = build_sale_notification(item, kind="new")
                embed = discord.Embed(
                    title="🔔 訂閱符合｜一般售屋",
                    description=note.description,
                    url=note.url,
                    color=discord.Color.blue(),
                )
        else:
            subscription = session.get(AuctionSubscription, subscription_id)
            item = session.get(AuctionCase, row.auction_case_id)
            active = subscription is not None and subscription.active and item is not None
            if active:
                current = item.current_round
                lines = [
                    f"類型：{CASE_TYPE_LABELS.get(item.case_type, '其他')}",
                    f"案號：{item.case_number}",
                    f"執行機關：{item.court_name}",
                    f"縣市區域：{item.city}{item.district}",
                    f"拍次：{current.round_number if current else '未提供'}",
                    (
                        f"底價：新臺幣 {current.floor_price_total_twd:,} 元"
                        if current
                        else "底價：未提供"
                    ),
                    f"點交：{'是' if item.is_deliverable else '否' if item.is_deliverable is False else '未確認'}",
                ]
                embed = discord.Embed(
                    title="🔔 訂閱符合｜法拍案件",
                    description="\n".join(lines),
                    url=item.announcement_url or None,
                    color=discord.Color.blue(),
                )
        if not active:
            row.status = "suppressed"
            row.last_error = "subscription inactive or item missing"
            session.commit()
            continue
        try:
            channel = channel_factory(row.channel_id)
            await channel.send(
                content=f"<@{subscription.discord_user_id}>",
                embed=embed,
            )
        except Exception as error:
            row.status = "failed"
            row.last_error = str(error)[:2000]
            row.attempt_count += 1
            failed += 1
        else:
            row.status = "delivered"
            row.delivered_at = utcnow()
            row.last_error = None
            row.attempt_count += 1
            delivered += 1
        session.commit()
    return SubscriptionDeliveryReport(attempted, delivered, failed)
