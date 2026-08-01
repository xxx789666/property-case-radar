"""Builds Discord notification text/embeds for the auction pipeline.

Formats follow the examples in taiwan_real_estate_radar.md sections 六
(new-case) and 九 (status-update). Every function that can be shown on a
*public* channel takes an explicit ``audience`` argument; the natural-
person 債務人/所有權人 fields are masked via
``notifications.auction_masking.display_debtor_owner`` whenever
``audience == "public"``, and ``AuctionStatusHistory.note`` (free text)
is never rendered on a public audience at all -- see spec section 七.

``AuctionNotificationRouter`` mirrors
``notifications.sale_notification.SaleNotificationRouter``'s shape: it is
constructed with the actual Discord channel objects (see
apps/discord_bot/main.py) and decides which of them a given case/event
should be posted to. Tests exercise it with ``unittest.mock.AsyncMock``
channels, never a live bot connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol

import discord

from database.models.auction import (
    ACTIVE_STATUSES,
    CASE_TYPE_LABELS,
    AuctionCase,
    AuctionStatus,
    AuctionStatusHistory,
)
from notifications.auction_masking import Audience, display_debtor_owner
from scoring.auction_score import SURFACE_DISCOUNT_DISCLAIMER, AuctionScore, risk_level

_STATUS_LABELS: dict[AuctionStatus, str] = {
    AuctionStatus.ANNOUNCED: "新增公告",
    AuctionStatus.CORRECTED: "更正公告",
    AuctionStatus.PRICE_CHANGED: "底價變更",
    AuctionStatus.DATE_CHANGED: "拍賣日期變更",
    AuctionStatus.FAILED: "流標",
    AuctionStatus.SUSPENDED: "停拍",
    AuctionStatus.WITHDRAWN: "撤回",
    AuctionStatus.AWARDED: "拍定",
}

def _fmt_money(value_twd: int | None) -> str:
    return f"{value_twd / 10_000:,.0f} 萬" if value_twd is not None else "未知"


def _fmt_unit_price(value_twd: int | None) -> str:
    return f"{value_twd / 10_000:.1f} 萬／坪" if value_twd is not None else "未知"


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value else "未定"


@dataclass(frozen=True)
class AuctionNotification:
    text: str
    audience: Audience


def build_new_case_notification(
    case: AuctionCase,
    market_unit_price_twd: int,
    score: AuctionScore,
    *,
    audience: Audience = "public",
) -> AuctionNotification:
    """⚖️ 新增法拍案件 -- spec section 六 example format."""
    current_round = case.current_round
    deliverable_text = "是" if case.is_deliverable else ("否" if case.is_deliverable is False else "未知")

    lines = [
        "⚖️ 新增法拍案件",
        "",
        f"法院：{case.court_name}",
        f"案號：{case.case_number}",
        f"區域：{case.city}{case.district}",
        f"類型：{CASE_TYPE_LABELS.get(case.case_type, case.case_type.value)}",
    ]
    if current_round is not None:
        lines += [
            f"拍次：第{current_round.round_number}拍",
            f"底價：{_fmt_money(current_round.floor_price_total_twd)}",
        ]
        if case.building_area_ping:
            lines.append(f"建坪：{case.building_area_ping:.1f} 坪")
        lines += [
            f"底價單價：{_fmt_unit_price(current_round.floor_unit_price_twd)}",
            f"區域成交均價：{_fmt_unit_price(market_unit_price_twd)}",
            f"表面折價率：{score.surface_discount_rate * 100:.1f}%",
        ]
    lines += [
        "",
        f"點交：{deliverable_text}",
        f"拍賣日期：{_fmt_date(current_round.auction_date) if current_round else '未定'}",
        f"風險評分：{risk_level(case)}",
        f"投資評分：{score.total:.0f}／100",
    ]
    if audience == "public":
        lines += ["", SURFACE_DISCOUNT_DISCLAIMER]
    return AuctionNotification(text="\n".join(lines), audience=audience)


def build_case_detail_text(
    case: AuctionCase,
    market_unit_price_twd: int,
    score: AuctionScore,
    *,
    audience: Audience = "public",
) -> AuctionNotification:
    """Full ``/auction detail`` response -- unlike the push notification above,
    this includes 債務人/所有權人 (spec section 七 fields), so it is the
    one place where masking actually changes the rendered text. Defaults
    to "public" (masked) so that a caller/adapter that forgets to pass
    ``audience`` fails safe -- callers that know they're in the private
    search channel (per discord 伺服器.txt) must opt in with
    ``audience="private"`` to see unmasked names.

    ``case.occupancy_note`` is free text entered by whoever recorded the
    case (a crawler operator, an admin) and may itself name a natural
    person (e.g. "占用人王小明拒絕遷讓" / "occupant John Smith refuses to
    vacate") -- exactly like ``AuctionStatusHistory.note`` there is no
    reliable way to auto-redact arbitrary free text, so it is simply
    omitted on a public audience rather than shown as-is.
    """
    base = build_new_case_notification(case, market_unit_price_twd, score, audience=audience)
    debtor_display, owner_display = display_debtor_owner(case.debtor, case.owner, audience=audience)
    extra = [
        "",
        f"債務人：{debtor_display or '未提供'}",
        f"所有權人：{owner_display or '未提供'}",
    ]
    if audience == "private":
        extra.append(f"占用情況：{case.occupancy_note or '未知'}")
    return AuctionNotification(text=base.text + "\n" + "\n".join(extra), audience=audience)


def build_status_update_notification(
    case: AuctionCase,
    event: AuctionStatusHistory,
    *,
    audience: Audience = "public",
) -> AuctionNotification:
    """⚠️ 法拍案件狀態更新 -- spec section 九 example format.

    ``event.note`` is free text written by whoever recorded the status
    change and may name a natural person -- there is no reliable way to
    auto-redact arbitrary free text the way masking.py can for the
    structured 債務人/所有權人 fields, so per spec section 七 the note is
    simply never rendered for a public audience.
    """
    from_label = _STATUS_LABELS.get(event.from_status) if event.from_status else "未知"
    to_label = _STATUS_LABELS[event.to_status]

    lines = [
        "⚠️ 法拍案件狀態更新",
        "",
        f"案號：{case.case_number}",
        f"原狀態：{from_label}",
        f"新狀態：{to_label}",
    ]
    if event.to_status == AuctionStatus.PRICE_CHANGED:
        if event.previous_floor_price_total_twd is not None and event.new_floor_price_total_twd is not None:
            lines += [
                "",
                f"底價：{_fmt_money(event.previous_floor_price_total_twd)} → "
                f"{_fmt_money(event.new_floor_price_total_twd)}",
            ]
        if event.previous_floor_unit_price_twd is not None and event.new_floor_unit_price_twd is not None:
            lines.append(
                f"底價單價：{_fmt_unit_price(event.previous_floor_unit_price_twd)} → "
                f"{_fmt_unit_price(event.new_floor_unit_price_twd)}"
            )
    if event.to_status == AuctionStatus.DATE_CHANGED and event.new_auction_date is not None:
        lines += ["", f"拍賣日期：{_fmt_date(event.previous_auction_date)} → {_fmt_date(event.new_auction_date)}"]
    current_round = case.current_round
    if (
        event.to_status not in (AuctionStatus.PRICE_CHANGED, AuctionStatus.DATE_CHANGED)
        and current_round is not None
        and current_round.auction_date
    ):
        lines += ["", f"原定日期：{current_round.auction_date.isoformat()}"]
    if event.to_status in (AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN):
        lines.append("請勿再依原日期前往投標。")
    if event.note and audience == "private":
        lines += ["", f"備註：{event.note}"]
    return AuctionNotification(text="\n".join(lines), audience=audience)


def _is_upcoming(case: AuctionCase, *, within_days: int, today: date | None = None) -> bool:
    if case.status not in ACTIVE_STATUSES:
        return False
    current_round = case.current_round
    if current_round is None or current_round.auction_date is None:
        return False
    reference = today or datetime.now().date()
    days_out = (current_round.auction_date - reference).days
    return 0 <= days_out <= within_days


# --- routing decisions (pure, no I/O) -----------------------------------
#
# Each returns a list of channel *kinds* -- see AuctionNotificationRouter
# below for the part that maps kinds to actual configured channels and
# sends. Kept separate so routing logic is testable without constructing
# a router or mocking channel objects.
#
# SUSPENDED/WITHDRAWN map onto the 停拍撤回 channel. FAILED (流標) and
# AWARDED (拍定/得標) have no dedicated channel in the current Discord
# config (discord 伺服器.txt only defines 新公告／即將開標／二拍三拍／
# 高分案件／停拍撤回) -- routing them into NEW would mislabel a
# failure/award as a fresh listing, so they get their own conservative
# handling instead of falling through to the default "still active"
# routing. A case whose status has already left
# ``database.models.auction.ACTIVE_STATUSES`` never gets a "new case"
# notification at all.


def channels_for_new_case(
    case: AuctionCase, score: AuctionScore, *, high_score_threshold: int = 80, upcoming_within_days: int = 7
) -> list[str]:
    if case.status not in ACTIVE_STATUSES:
        return []
    # The new-announcement channel is reserved for one daily county/city
    # aggregate. Individual cases continue to route to the specialised
    # round/high-score/upcoming channels below.
    kinds: list[str] = []
    if score.total >= high_score_threshold:
        kinds.append("high_score")
    return kinds


def channels_for_status_event(
    case: AuctionCase, event: AuctionStatusHistory, *, upcoming_within_days: int = 7
) -> list[str]:
    if event.to_status in (AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN):
        return ["suspended_withdrawn"]
    if event.to_status == AuctionStatus.AWARDED:
        return []
    if event.to_status == AuctionStatus.FAILED:
        return []
    return []


class MessageChannel(Protocol):
    async def send(
        self, *, embed: discord.Embed, content: str | None = None
    ) -> object: ...

    async def edit(self, *, message_id: int, embed: discord.Embed) -> object: ...

    async def find_message_id(self, *, embed_title: str) -> int | None: ...


_KIND_TO_CHANNEL_ATTR = {
    "new": "new_channel",
    "round": "round_channel",
    "high_score": "high_score_channel",
    "upcoming": "upcoming_channel",
    "suspended_withdrawn": "suspended_channel",
}


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of attempting to send to exactly one destination (channel kind)."""

    kind: str
    success: bool
    error: str | None = None


class AuctionNotificationRouter:
    """Maps ``channels_for_new_case``/``channels_for_status_event``'s decisions to
    actual configured Discord channels and sends. See those functions'
    module-level docstring for the routing rules themselves.

    Every send is per-destination: ``send_new_case``/``send_status_event``
    each target exactly one channel kind and never raise -- a failure on
    one destination (bad channel ID, rate limit, network blip, ...) is
    caught and returned as a failed ``DeliveryOutcome``, so it can never
    prevent an attempt at any other destination for the same case/event.
    ``publish_new_case``/``publish_status_event`` are the convenience
    wrappers that compute which kinds apply and attempt all of them,
    aggregating every outcome (including failures) rather than
    short-circuiting on the first one.
    """

    def __init__(
        self,
        *,
        new_channel: MessageChannel,
        upcoming_channel: MessageChannel,
        round_channel: MessageChannel,
        high_score_channel: MessageChannel,
        suspended_channel: MessageChannel,
        high_score_threshold: int = 80,
        upcoming_within_days: int = 7,
        channel_factory: Callable[[int], MessageChannel] | None = None,
    ) -> None:
        self.new_channel = new_channel
        self.upcoming_channel = upcoming_channel
        self.round_channel = round_channel
        self.high_score_channel = high_score_channel
        self.suspended_channel = suspended_channel
        self.high_score_threshold = high_score_threshold
        self.upcoming_within_days = upcoming_within_days
        self.channel_factory = channel_factory

    def subscription_channel(self, channel_id: int) -> MessageChannel:
        if self.channel_factory is None:
            raise RuntimeError("subscription channel routing is not configured")
        return self.channel_factory(channel_id)

    async def send_new_case(
        self, kind: str, case: AuctionCase, market_unit_price_twd: int, score: AuctionScore
    ) -> DeliveryOutcome:
        """Send a "new case" notification to exactly the ``kind`` destination. Never raises."""
        channel = getattr(self, _KIND_TO_CHANNEL_ATTR[kind])
        try:
            await self._send_new(channel, case, market_unit_price_twd, score)
            return DeliveryOutcome(kind=kind, success=True)
        except Exception as exc:  # noqa: BLE001 -- one destination's failure must never affect any other
            return DeliveryOutcome(kind=kind, success=False, error=str(exc))

    async def send_status_event(self, kind: str, case: AuctionCase, event: AuctionStatusHistory) -> DeliveryOutcome:
        """Send a status-update notification to exactly the ``kind`` destination. Never raises."""
        channel = getattr(self, _KIND_TO_CHANNEL_ATTR[kind])
        try:
            await self._send_status(channel, case, event)
            return DeliveryOutcome(kind=kind, success=True)
        except Exception as exc:  # noqa: BLE001 -- one destination's failure must never affect any other
            return DeliveryOutcome(kind=kind, success=False, error=str(exc))

    async def publish_new_case(
        self, case: AuctionCase, market_unit_price_twd: int, score: AuctionScore
    ) -> list[DeliveryOutcome]:
        kinds = channels_for_new_case(
            case, score, high_score_threshold=self.high_score_threshold, upcoming_within_days=self.upcoming_within_days
        )
        return [await self.send_new_case(kind, case, market_unit_price_twd, score) for kind in kinds]

    async def publish_status_event(self, case: AuctionCase, event: AuctionStatusHistory) -> list[DeliveryOutcome]:
        kinds = channels_for_status_event(case, event, upcoming_within_days=self.upcoming_within_days)
        return [await self.send_status_event(kind, case, event) for kind in kinds]

    @staticmethod
    async def _send_new(
        channel: MessageChannel, case: AuctionCase, market_unit_price_twd: int, score: AuctionScore
    ) -> None:
        note = build_new_case_notification(case, market_unit_price_twd, score, audience="public")
        embed = discord.Embed(title="⚖️ 新增法拍案件", description=note.text, color=discord.Color.orange())
        await channel.send(embed=embed)

    @staticmethod
    async def _send_status(channel: MessageChannel, case: AuctionCase, event: AuctionStatusHistory) -> None:
        note = build_status_update_notification(case, event, audience="public")
        embed = discord.Embed(title="⚠️ 法拍案件狀態更新", description=note.text, color=discord.Color.red())
        await channel.send(embed=embed)
