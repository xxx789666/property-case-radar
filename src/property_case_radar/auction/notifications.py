"""Builds Discord notification text for the auction pipeline.

Formats follow the examples in taiwan_real_estate_radar.md sections 六
(new-case) and 九 (status-update). Every function that can be shown on a
*public* channel takes an explicit ``audience`` argument and masks
natural-person fields via auction/masking.py when ``audience ==
"public"`` -- see spec section 七's note about not broadcasting PII.

Channel routing (``channels_for_new_case`` / ``channels_for_status_event``)
is a pure function of case data; it does not send anything. Actually
delivering to Discord (discord.py client, webhook, etc.) is intentionally
out of scope for this vertical slice -- see docs/auction_pipeline.md for
the integration point. Tests exercise these functions with plain
strings/fakes, never a live bot connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from property_case_radar.auction import channels
from property_case_radar.auction.masking import Audience, view_for_audience
from property_case_radar.auction.models import AuctionCase, AuctionStatus, AuctionStatusEvent, CaseType
from property_case_radar.auction.scoring import SURFACE_DISCOUNT_DISCLAIMER, ScoreBreakdown, risk_level
from property_case_radar.auction.state_machine import ACTIVE_STATES
from property_case_radar.shared.market_prices import RegionalMarketPrice

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

_CASE_TYPE_LABELS: dict[CaseType, str] = {
    CaseType.RESIDENTIAL: "住宅",
    CaseType.STOREFRONT: "店面",
    CaseType.LAND: "土地",
    CaseType.OFFICE_FACTORY: "廠辦",
    CaseType.OTHER: "其他",
}


def _fmt_money_wan(value: float | None) -> str:
    return f"{value:,.0f} 萬" if value is not None else "未知"


def _fmt_unit_price(value: float | None) -> str:
    return f"{value:.1f} 萬／坪" if value is not None else "未知"


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value else "未定"




@dataclass(frozen=True)
class NewCaseNotification:
    text: str
    audience: Audience


def build_new_case_notification(
    case: AuctionCase,
    market_price: RegionalMarketPrice,
    score: ScoreBreakdown,
    *,
    audience: Audience = "public",
) -> NewCaseNotification:
    """⚖️ 新增法拍案件 -- spec section 六 example format."""
    view = view_for_audience(case, audience)
    current_round = view.current_round
    deliverable_text = "是" if view.is_deliverable else ("否" if view.is_deliverable is False else "未知")

    lines = [
        "⚖️ 新增法拍案件",
        "",
        f"法院：{view.court_name}",
        f"案號：{view.case_number}",
        f"區域：{view.city}{view.district}",
        f"類型：{_CASE_TYPE_LABELS.get(view.case_type, view.case_type.value)}",
    ]
    if current_round is not None:
        lines += [
            f"拍次：第{current_round.round_number}拍",
            f"底價：{_fmt_money_wan(current_round.floor_price_total)}",
        ]
        if view.building_area_ping:
            lines.append(f"建坪：{view.building_area_ping:.1f} 坪")
        lines += [
            f"底價單價：{_fmt_unit_price(current_round.floor_unit_price)}",
            f"區域成交均價：{_fmt_unit_price(market_price.avg_unit_price_per_ping)}",
            f"表面折價率：{score.surface_discount_rate * 100:.1f}%",
        ]
    lines += [
        "",
        f"點交：{deliverable_text}",
        f"拍賣日期：{_fmt_date(current_round.auction_date) if current_round else '未定'}",
        f"風險評分：{_risk_level_text(case)}",
        f"投資評分：{score.investment_score:.0f}／100",
    ]
    if audience == "public":
        lines += ["", SURFACE_DISCOUNT_DISCLAIMER]
    return NewCaseNotification(text="\n".join(lines), audience=audience)


def build_case_detail_text(
    case: AuctionCase,
    market_price: RegionalMarketPrice,
    score: ScoreBreakdown,
    *,
    audience: Audience = "public",
) -> NewCaseNotification:
    """Full ``/auction detail`` response -- unlike the push notification above,
    this includes 債務人/所有權人 (spec section 七 fields), so it is the
    one place where masking actually changes the rendered text. Defaults
    to "public" (masked) so that a caller/adapter that forgets to pass
    ``audience`` fails safe -- callers that know they're in the private
    search channel (per discord 伺服器.txt) must opt in with
    ``audience="private"`` to see unmasked names.
    """
    base = build_new_case_notification(case, market_price, score, audience=audience)
    view = view_for_audience(case, audience)
    extra = [
        "",
        f"債務人：{view.debtor or '未提供'}",
        f"所有權人：{view.owner or '未提供'}",
        f"占用情況：{view.occupancy_note or '未知'}",
    ]
    return NewCaseNotification(text=base.text + "\n" + "\n".join(extra), audience=audience)


def build_status_update_notification(
    case: AuctionCase,
    event: AuctionStatusEvent,
    *,
    audience: Audience = "public",
) -> NewCaseNotification:
    """⚠️ 法拍案件狀態更新 -- spec section 九 example format.

    ``event.note`` is free text written by whoever recorded the status
    change (a crawler operator, an admin command, ...) and may name a
    natural person (e.g. "債務人王小明已提出異議") -- there is no reliable
    way to auto-redact arbitrary free text the way masking.py can for the
    structured 債務人/所有權人 fields, so per spec section 七 the note is
    simply never rendered for a public audience. Private/detail views
    (the search channel, or a user's own lookup) still see it in full.
    """
    view = view_for_audience(case, audience)
    from_label = _STATUS_LABELS.get(event.from_status) if event.from_status else "未知"
    to_label = _STATUS_LABELS[event.to_status]

    lines = [
        "⚠️ 法拍案件狀態更新",
        "",
        f"案號：{view.case_number}",
        f"原狀態：{from_label}",
        f"新狀態：{to_label}",
    ]
    if event.to_status == AuctionStatus.PRICE_CHANGED:
        if event.previous_floor_price_total is not None and event.new_floor_price_total is not None:
            lines += [
                "",
                f"底價：{_fmt_money_wan(event.previous_floor_price_total)} → "
                f"{_fmt_money_wan(event.new_floor_price_total)}",
            ]
        if event.previous_floor_unit_price is not None and event.new_floor_unit_price is not None:
            lines.append(
                f"底價單價：{_fmt_unit_price(event.previous_floor_unit_price)} → "
                f"{_fmt_unit_price(event.new_floor_unit_price)}"
            )
    if event.to_status == AuctionStatus.DATE_CHANGED and event.new_auction_date is not None:
        lines += [
            "",
            f"拍賣日期：{_fmt_date(event.previous_auction_date)} → {_fmt_date(event.new_auction_date)}",
        ]
    current_round = view.current_round
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
    return NewCaseNotification(text="\n".join(lines), audience=audience)


def _risk_level_text(case: AuctionCase) -> str:
    return risk_level(case)


def channels_for_new_case(case: AuctionCase, score: ScoreBreakdown) -> list[channels.AuctionChannel]:
    """Which public channels a brand-new case should be posted to."""
    if case.status not in ACTIVE_STATES:
        # A "new case" notification should never fire for a case that is
        # already resolved (SUSPENDED/WITHDRAWN/AWARDED) or mid-FAILED --
        # those go through channels_for_status_event instead, if at all.
        return []
    targets = [channels.NEW_ANNOUNCEMENT]
    if case.round_number is not None and case.round_number >= 2:
        targets.append(channels.SECOND_THIRD_ROUND)
    if score.investment_score >= channels.HIGH_SCORE_THRESHOLD:
        targets.append(channels.HIGH_SCORE)
    if _is_upcoming(case):
        targets.append(channels.UPCOMING_AUCTION)
    return targets


def channels_for_status_event(case: AuctionCase, event: AuctionStatusEvent) -> list[channels.AuctionChannel]:
    """Which public channels a status-change event should be posted to.

    SUSPENDED/WITHDRAWN map onto the 停拍撤回 channel. FAILED (流標) and
    AWARDED (拍定/得標) have no dedicated channel in the current Discord
    config (discord 伺服器.txt only defines 新公告／即將開標／二拍三拍／
    高分案件／停拍撤回) -- routing them into NEW_ANNOUNCEMENT would
    mislabel a failure/award as a fresh listing, so they get their own
    conservative handling below instead of falling through to the
    default "still active" routing.
    """
    if event.to_status in (AuctionStatus.SUSPENDED, AuctionStatus.WITHDRAWN):
        return [channels.SUSPENDED_WITHDRAWN]
    if event.to_status == AuctionStatus.AWARDED:
        # No "案件已拍定" channel exists; do not post a resolved case into
        # channels that imply it is still biddable.
        return []
    if event.to_status == AuctionStatus.FAILED:
        # Only surface a failure once the case has actually reached round
        # 2+ (the 二拍三拍 channel's audience already expects round churn);
        # a round-1 failure has no matching channel.
        return [channels.SECOND_THIRD_ROUND] if (case.round_number or 0) >= 2 else []
    # Remaining reachable statuses here are the "still active" amendment
    # states (CORRECTED/PRICE_CHANGED/DATE_CHANGED) and the round-advance
    # ANNOUNCED -- route them like a still-live case.
    targets = [channels.NEW_ANNOUNCEMENT]
    if case.round_number is not None and case.round_number >= 2:
        targets.append(channels.SECOND_THIRD_ROUND)
    if _is_upcoming(case):
        targets.append(channels.UPCOMING_AUCTION)
    return targets


def _is_upcoming(case: AuctionCase, *, today: date | None = None) -> bool:
    if case.status not in ACTIVE_STATES:
        return False
    current_round = case.current_round
    if current_round is None or current_round.auction_date is None:
        return False
    reference = today or datetime.now().date()
    days_out = (current_round.auction_date - reference).days
    return 0 <= days_out <= channels.UPCOMING_WITHIN_DAYS
