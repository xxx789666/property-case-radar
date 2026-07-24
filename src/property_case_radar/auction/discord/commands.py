"""``/auction search|subscribe|latest|detail|schedule|risk|unsubscribe``.

Spec: taiwan_real_estate_radar.md section 六 (command list + examples).

These handlers are plain Python methods on ``AuctionCommandHandlers``,
not discord.py ``app_commands`` -- see discord/__init__.py for why. Each
returns a ``CommandResponse(text, audience)`` string bundle; a thin
adapter can later map these onto real Discord interactions (e.g.
``await interaction.response.send_message(response.text, ephemeral=...)``)
without touching this module's logic.

Default ``audience`` is "public": these handlers are framework-agnostic
and don't know which Discord channel (or ephemeral-vs-not context) they
were actually invoked from -- that's a real adapter's job to determine
and pass in. If a future discord.py adapter forgets to pass ``audience``
at all, defaulting to "public" fails safe (masked/PII-free) instead of
silently leaking 債務人/所有權人 into whatever channel it was called
from. Callers that positively know they're in the private 法拍案件-搜尋
channel (per discord 伺服器.txt) must opt in with ``audience="private"``
to see unmasked names.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from property_case_radar.auction.masking import Audience, view_for_audience
from property_case_radar.auction.models import AuctionFilter, AuctionSubscription
from property_case_radar.auction.notifications import build_case_detail_text
from property_case_radar.auction.repository import AuctionRepository
from property_case_radar.auction.scoring import (
    delivery_score,
    ownership_score,
    risk_level,
    risk_score_0_100,
    score_case,
)
from property_case_radar.auction.state_machine import ACTIVE_STATES
from property_case_radar.shared.market_prices import MarketPriceProvider

_CASE_TYPE_LABELS = {
    "residential": "住宅",
    "storefront": "店面",
    "land": "土地",
    "office_factory": "廠辦",
    "other": "其他",
}


@dataclass(frozen=True)
class CommandResponse:
    text: str
    audience: Audience = "public"


def _case_summary_line(case, audience: Audience) -> str:
    view = view_for_audience(case, audience)
    current = view.current_round
    round_text = f"第{current.round_number}拍" if current else "尚無拍次資料"
    floor_text = f"底價 {current.floor_price_total:,.0f} 萬" if current and current.floor_price_total else ""
    score_text = f"投資評分 {view.investment_score:.0f}" if view.investment_score is not None else ""
    parts = [f"{view.court_name} {view.case_number}", f"{view.city}{view.district}", round_text, floor_text, score_text]
    return "・".join(p for p in parts if p)


def _describe_filters(filters: AuctionFilter) -> str:
    bits = []
    if filters.city:
        bits.append(f"區域：{filters.city}{filters.district or ''}")
    if filters.case_type:
        bits.append(f"類型：{_CASE_TYPE_LABELS.get(filters.case_type.value, filters.case_type.value)}")
    if filters.floor_price_max is not None:
        bits.append(f"底價上限：{filters.floor_price_max:,.0f} 萬")
    if filters.min_round is not None:
        bits.append(f"拍次：{filters.min_round}拍以上")
    if filters.require_deliverable is not None:
        bits.append(f"點交：{'是' if filters.require_deliverable else '否'}")
    if filters.min_investment_score is not None:
        bits.append(f"最低投資評分：{filters.min_investment_score:.0f}")
    return "、".join(bits) if bits else "（無篩選條件）"


class AuctionCommandHandlers:
    """Implements the seven /auction subcommands against an AuctionRepository."""

    def __init__(self, repository: AuctionRepository, market_prices: MarketPriceProvider) -> None:
        self._repo = repository
        self._market_prices = market_prices

    # /auction search
    def search(self, filters: AuctionFilter, *, audience: Audience = "public", limit: int = 10) -> CommandResponse:
        matches = [c for c in self._repo.list_cases() if filters.matches(c)]
        matches.sort(key=lambda c: c.updated_at, reverse=True)
        matches = matches[:limit]
        if not matches:
            return CommandResponse(f"查無符合條件的法拍案件。\n篩選條件：{_describe_filters(filters)}", audience)
        lines = [f"🔎 符合條件的法拍案件（{len(matches)} 筆）", f"篩選條件：{_describe_filters(filters)}", ""]
        lines += [_case_summary_line(c, audience) for c in matches]
        return CommandResponse("\n".join(lines), audience)

    # /auction subscribe
    def subscribe(
        self,
        user_id: str,
        filters: AuctionFilter,
        *,
        channel_id: str | None = None,
        subscription_id: str | None = None,
    ) -> CommandResponse:
        sub_id = subscription_id or str(uuid.uuid4())
        subscription = AuctionSubscription(
            subscription_id=sub_id, user_id=user_id, filters=filters, channel_id=channel_id
        )
        self._repo.add_subscription(subscription)
        return CommandResponse(
            f"✅ 已建立法拍案件訂閱（編號：{sub_id}）\n{_describe_filters(filters)}", "private"
        )

    # /auction unsubscribe
    def unsubscribe(self, user_id: str, subscription_id: str) -> CommandResponse:
        subscription = self._repo.get_subscription(subscription_id)
        if subscription is None or subscription.user_id != user_id or not subscription.active:
            return CommandResponse(f"找不到訂閱編號 {subscription_id}。", "private")
        self._repo.remove_subscription(subscription_id)
        return CommandResponse(f"🗑️ 已取消訂閱（編號：{subscription_id}）", "private")

    # /auction latest
    def latest(self, *, limit: int = 5, audience: Audience = "public") -> CommandResponse:
        cases = sorted(self._repo.list_cases(), key=lambda c: c.first_seen_at, reverse=True)[:limit]
        if not cases:
            return CommandResponse("目前沒有法拍案件資料。", audience)
        lines = [f"🆕 最新法拍案件（{len(cases)} 筆）", ""]
        lines += [_case_summary_line(c, audience) for c in cases]
        return CommandResponse("\n".join(lines), audience)

    # /auction detail
    def detail(self, court_name: str, case_number: str, *, audience: Audience = "public") -> CommandResponse:
        case = self._repo.get_case_by_number(court_name, case_number)
        if case is None:
            return CommandResponse(f"查無案件：{court_name} {case_number}", audience)
        market = self._market_prices.get_regional_average(case.city, case.district)
        if market is None or case.current_round is None:
            view = view_for_audience(case, audience)
            return CommandResponse(
                f"案號：{view.case_number}\n（尚無區域行情或拍次資料，無法計算折價率／評分）", audience
            )
        score = score_case(case, market.avg_unit_price_per_ping)
        notification = build_case_detail_text(case, market, score, audience=audience)
        return CommandResponse(notification.text, audience)

    # /auction schedule
    def schedule(
        self, *, within_days: int = 7, audience: Audience = "public", today: date | None = None
    ) -> CommandResponse:
        reference = today or date.today()
        upcoming = []
        for case in self._repo.list_cases():
            if case.status not in ACTIVE_STATES:
                # Exclude FAILED (between rounds, no confirmed date) and
                # every terminal status (SUSPENDED/WITHDRAWN/AWARDED) --
                # none of these are still going to auction on the date
                # recorded on the current round.
                continue
            current = case.current_round
            if current is None or current.auction_date is None:
                continue
            days_out = (current.auction_date - reference).days
            if 0 <= days_out <= within_days:
                upcoming.append(case)
        upcoming.sort(key=lambda c: c.current_round.auction_date)
        if not upcoming:
            return CommandResponse(f"未來 {within_days} 天內沒有即將開標的法拍案件。", audience)
        lines = [f"📅 未來 {within_days} 天內開標案件（{len(upcoming)} 筆）", ""]
        for case in upcoming:
            view = view_for_audience(case, audience)
            lines.append(f"{view.current_round.auction_date} - {_case_summary_line(case, audience)}")
        return CommandResponse("\n".join(lines), audience)

    # /auction risk
    def risk(self, court_name: str, case_number: str, *, audience: Audience = "public") -> CommandResponse:
        case = self._repo.get_case_by_number(court_name, case_number)
        if case is None:
            return CommandResponse(f"查無案件：{court_name} {case_number}", audience)
        view = view_for_audience(case, audience)
        lines = [
            f"⚠️ 風險評估：{view.case_number}",
            "",
            f"點交狀態：{view.occupancy_status.value}（{delivery_score(case):.0f}／20 分）",
            f"占用情況：{view.occupancy_note or '未知'}",
            f"產權：{view.ownership_type.value}（{ownership_score(case):.0f}／20 分）",
            f"是否有增建：{'是' if view.has_unregistered_addition else '否'}",
            "",
            f"風險評分：{risk_score_0_100(case):.0f}／100（{risk_level(case)}）",
        ]
        return CommandResponse("\n".join(lines), audience)
