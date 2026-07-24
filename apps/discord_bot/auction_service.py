"""Business logic + DB session management for the /auction Discord commands.

Mirrors ``apps.discord_bot.service.HouseCommandService``'s shape: a plain
class wrapping a ``sessionmaker``, opening one session per call.
Framework-agnostic (no ``discord.py`` import) so it's directly testable
without a live bot connection -- see ``apps/discord_bot/auction_cog.py``
for the thin discord.py adapter on top of this.

Default ``audience`` is "public" on every read method: this service
doesn't know which Discord channel (or ephemeral-vs-not context) it was
actually called from -- that's ``AuctionCog``'s job, which only passes
``audience="private"`` after its own channel guard has already verified
the interaction is in the private 法拍案件-搜尋 channel. If a future
caller (an admin script, a different adapter) forgets to pass
``audience`` at all, defaulting to "public" fails safe (masked/PII-free)
instead of silently leaking 債務人/所有權人.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from database.models.auction import CASE_TYPE_LABELS, AuctionCase, CaseType
from database.models.common import MarketPrice
from database.repositories.auction import AuctionRepository, AuctionSearchFilters, AuctionSubscriptionRepository
from notifications.auction_masking import Audience
from notifications.auction_notification import build_case_detail_text
from scoring.auction_score import delivery_score, ownership_score, risk_level, risk_score_0_100, score_auction_case


@dataclass(frozen=True)
class AuctionSearchInput:
    city: str | None = None
    district: str | None = None
    case_type: CaseType | None = None
    max_floor_price_wan: int | None = None  # user-facing input in 萬, converted to TWD internally
    min_round: int | None = None
    require_deliverable: bool | None = None
    min_investment_score: Decimal | None = None


@dataclass(frozen=True)
class CommandResponse:
    text: str
    audience: Audience = "public"


def _summary_line(case: AuctionCase, audience: Audience) -> str:
    # Summary lines never include 債務人/所有權人, so `audience` doesn't
    # currently change their content -- kept as a parameter anyway so
    # every render path in this module has the same signature shape and
    # callers never need to special-case "does this one need it."
    del audience
    current = case.current_round
    round_text = f"第{current.round_number}拍" if current else "尚無拍次資料"
    floor_text = f"底價 {current.floor_price_total_twd / 10_000:,.0f} 萬" if current else ""
    score_text = f"投資評分 {case.investment_score:.0f}" if case.investment_score is not None else ""
    parts = [f"{case.court_name} {case.case_number}", f"{case.city}{case.district}", round_text, floor_text, score_text]
    return "・".join(p for p in parts if p)


def _lookup_market(session: Session, case: AuctionCase) -> MarketPrice | None:
    building_type = CASE_TYPE_LABELS.get(case.case_type, "其他")
    return session.scalar(
        select(MarketPrice).where(
            MarketPrice.city == case.city,
            MarketPrice.district == case.district,
            MarketPrice.building_type == building_type,
        )
    )


class AuctionCommandService:
    def __init__(self, factory: sessionmaker[Session]):
        self.factory = factory

    def _to_filters(self, values: AuctionSearchInput, limit: int) -> AuctionSearchFilters:
        return AuctionSearchFilters(
            city=values.city,
            district=values.district,
            case_type=values.case_type,
            max_floor_price_twd=values.max_floor_price_wan * 10_000 if values.max_floor_price_wan else None,
            min_round=values.min_round,
            require_deliverable=values.require_deliverable,
            min_investment_score=values.min_investment_score,
            limit=limit,
        )

    # /auction search
    def search(self, values: AuctionSearchInput, *, limit: int = 10, audience: Audience = "public") -> CommandResponse:
        with self.factory() as session:
            cases = AuctionRepository(session).search(self._to_filters(values, limit))
            if not cases:
                return CommandResponse("查無符合條件的法拍案件。", audience)
            return CommandResponse("\n".join(_summary_line(c, audience) for c in cases), audience)

    # /auction subscribe
    def subscribe(self, discord_user_id: int, values: AuctionSearchInput) -> CommandResponse:
        with self.factory() as session:
            item = AuctionSubscriptionRepository(session).create(
                discord_user_id=discord_user_id,
                city=values.city,
                district=values.district,
                case_type=values.case_type,
                max_floor_price_twd=values.max_floor_price_wan * 10_000 if values.max_floor_price_wan else None,
                min_round=values.min_round,
                require_deliverable=values.require_deliverable,
                min_investment_score=values.min_investment_score,
            )
            session.commit()
            return CommandResponse(f"已建立法拍案件訂閱 #{item.id}。", "private")

    # /auction unsubscribe
    def unsubscribe(self, discord_user_id: int, subscription_id: int) -> CommandResponse:
        with self.factory() as session:
            changed = AuctionSubscriptionRepository(session).deactivate(discord_user_id, subscription_id)
            session.commit()
            return CommandResponse("已取消訂閱。" if changed else "找不到可取消的訂閱。", "private")

    # /auction latest
    def latest(self, *, limit: int = 5, audience: Audience = "public") -> CommandResponse:
        with self.factory() as session:
            cases = AuctionRepository(session).latest(limit)
            if not cases:
                return CommandResponse("目前沒有法拍案件資料。", audience)
            return CommandResponse("\n".join(_summary_line(c, audience) for c in cases), audience)

    # /auction detail
    def detail(self, court_name: str, case_number: str, *, audience: Audience = "public") -> CommandResponse:
        with self.factory() as session:
            case = AuctionRepository(session).get_by_case_number(court_name, case_number)
            if case is None:
                return CommandResponse(f"查無案件：{court_name} {case_number}", audience)
            if case.current_round is None:
                return CommandResponse(f"案號：{case.case_number}\n（尚無拍次資料，無法計算折價率／評分）", audience)
            market = _lookup_market(session, case)
            if market is None:
                return CommandResponse(f"案號：{case.case_number}\n（尚無區域行情資料，無法計算折價率／評分）", audience)
            score = score_auction_case(case, market.average_unit_price_twd)
            note = build_case_detail_text(case, market.average_unit_price_twd, score, audience=audience)
            return CommandResponse(note.text, audience)

    # /auction schedule
    def schedule(
        self, *, within_days: int = 7, audience: Audience = "public", today: date | None = None
    ) -> CommandResponse:
        with self.factory() as session:
            cases = AuctionRepository(session).upcoming(within_days=within_days, today=today)
            if not cases:
                return CommandResponse(f"未來 {within_days} 天內沒有即將開標的法拍案件。", audience)
            lines = [f"{c.current_round.auction_date} - {_summary_line(c, audience)}" for c in cases]
            return CommandResponse("\n".join(lines), audience)

    # /auction risk
    def risk(self, court_name: str, case_number: str, *, audience: Audience = "public") -> CommandResponse:
        with self.factory() as session:
            case = AuctionRepository(session).get_by_case_number(court_name, case_number)
            if case is None:
                return CommandResponse(f"查無案件：{court_name} {case_number}", audience)
            lines = [
                f"⚠️ 風險評估：{case.case_number}",
                "",
                f"點交狀態：{case.occupancy_status.value}（{delivery_score(case)}／20 分）",
                f"占用情況：{case.occupancy_note or '未知'}",
                f"產權：{case.ownership_type.value}（{ownership_score(case)}／20 分）",
                f"是否有增建：{'是' if case.has_unregistered_addition else '否'}",
                "",
                f"風險評分：{risk_score_0_100(case)}／100（{risk_level(case)}）",
            ]
            return CommandResponse("\n".join(lines), audience)
