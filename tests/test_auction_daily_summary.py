from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from apps.services.auction_daily_summary import (
    build_daily_summary_embed,
    daily_auction_counts,
    deliver_daily_auction_summary,
)
from database.models.auction import AuctionCase, AuctionRound
from database.models.common import NotificationLog

TAIPEI = ZoneInfo("Asia/Taipei")


def _case(
    number: str,
    city: str,
    seen_at: datetime,
    *,
    round_number: int | None = None,
) -> AuctionCase:
    return AuctionCase(
        court_name="法務部行政執行署",
        case_number=number,
        city=city,
        first_seen_at=seen_at,
        updated_at=seen_at,
        rounds=(
            [
                AuctionRound(
                    round_number=round_number,
                    floor_price_total_twd=1,
                    floor_unit_price_twd=1,
                )
            ]
            if round_number is not None
            else []
        ),
    )


def test_daily_counts_use_taipei_day_and_include_zero_regions(session_factory) -> None:
    day = date(2026, 7, 27)
    with session_factory() as session:
        session.add_all(
            [
                _case("summary-1", "臺北市", datetime(2026, 7, 27, 0, 1, tzinfo=TAIPEI)),
                _case("summary-2", "台北市", datetime(2026, 7, 27, 23, 59, tzinfo=TAIPEI)),
                _case("summary-old", "高雄市", datetime(2026, 7, 26, 23, 59, tzinfo=TAIPEI)),
            ]
        )
        session.commit()

        counts = daily_auction_counts(session, day)

    assert counts["臺北市"] == 2
    assert counts["高雄市"] == 0
    assert counts["連江縣"] == 0


def test_daily_round_counts_use_exact_current_round(session_factory) -> None:
    day = date(2026, 7, 27)
    seen_at = datetime(2026, 7, 27, 12, 0, tzinfo=TAIPEI)
    with session_factory() as session:
        first_round = _case("round-summary-1", "臺南市", seen_at, round_number=1)
        second_round = _case("round-summary-2", "臺南市", seen_at, round_number=2)
        third_round = _case("round-summary-3", "高雄市", seen_at, round_number=2)
        third_round.rounds.append(
            AuctionRound(
                round_number=3,
                floor_price_total_twd=1,
                floor_unit_price_twd=1,
            )
        )
        session.add_all([first_round, second_round, third_round])
        session.commit()

        round_two = daily_auction_counts(session, day, round_number=2)
        round_three = daily_auction_counts(session, day, round_number=3)

    assert round_two["臺南市"] == 1
    assert round_two["高雄市"] == 0
    assert round_three["臺南市"] == 0
    assert round_three["高雄市"] == 1


def test_daily_summary_embed_lists_counties_and_total() -> None:
    counts = {"臺北市": 3, "臺南市": 2}

    embed = build_daily_summary_embed(date(2026, 7, 27), counts)

    assert embed.title == "📊 今日各縣市新增法拍｜2026-07-27"
    assert "臺北市：3 筆" in embed.description
    assert "臺南市：2 筆" in embed.description
    assert embed.footer.text == "今日合計：5 筆"

    round_embed = build_daily_summary_embed(
        date(2026, 7, 27),
        counts,
        round_number=2,
    )
    assert round_embed.title == "📊 今日各縣市二拍新增法拍｜2026-07-27"

    third_round_embed = build_daily_summary_embed(
        date(2026, 7, 27),
        counts,
        round_number=3,
    )
    assert third_round_embed.title == "📊 今日各縣市三拍新增法拍｜2026-07-27"


@pytest.mark.asyncio
async def test_daily_summary_delivery_is_idempotent(session_factory) -> None:
    day = date(2026, 7, 27)
    channel = AsyncMock()
    with session_factory() as session:
        session.add(
            _case("summary-send", "桃園市", datetime(2026, 7, 27, 12, 0, tzinfo=TAIPEI))
        )
        session.commit()

        first = await deliver_daily_auction_summary(session, channel, 123, day=day)
        second = await deliver_daily_auction_summary(session, channel, 123, day=day)
        round_two = await deliver_daily_auction_summary(
            session,
            channel,
            456,
            day=day,
            round_number=2,
        )
        round_two_duplicate = await deliver_daily_auction_summary(
            session,
            channel,
            456,
            day=day,
            round_number=2,
        )
        round_three = await deliver_daily_auction_summary(
            session,
            channel,
            456,
            day=day,
            round_number=3,
        )

        assert first.delivered is True
        assert first.total == 1
        assert second.skipped_duplicate is True
        assert round_two.delivered is True
        assert round_two_duplicate.skipped_duplicate is True
        assert round_three.delivered is True
        assert channel.send.await_count == 3
        rows = session.scalars(select(NotificationLog)).all()
        assert {row.kind for row in rows} == {
            "daily_summary",
            "daily_round_2_summary",
            "daily_round_3_summary",
        }
        assert all(row.status == "delivered" for row in rows)
