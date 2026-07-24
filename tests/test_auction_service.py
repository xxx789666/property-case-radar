"""apps.discord_bot.auction_service.AuctionCommandService, exercised
against a real SQLite session (no discord.py import needed -- this
module is framework-agnostic by design).
"""

from datetime import datetime, timezone
from decimal import Decimal

from apps.discord_bot.auction_service import AuctionCommandService, AuctionSearchInput
from database.models.auction import AuctionCase, AuctionRound, CaseType, OccupancyStatus, OwnershipType
from database.models.common import MarketPrice


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _seed(session, *, occupancy_note: str = "", debtor: str = "王小明", owner: str = "王小明") -> AuctionCase:
    case = AuctionCase(
        court_name="桃園地方法院",
        case_number="115XXX",
        city="桃園市",
        district="中壢區",
        case_type=CaseType.RESIDENTIAL,
        ownership_type=OwnershipType.FULL,
        occupancy_status=OccupancyStatus.OCCUPIED_DELIVERABLE,
        occupancy_note=occupancy_note,
        debtor=debtor,
        owner=owner,
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        investment_score=Decimal("88"),
        rounds=[AuctionRound(round_number=2, floor_price_total_twd=9_800_000, floor_unit_price_twd=232_000)],
    )
    session.add(case)
    session.commit()
    return case


class TestRiskCommandPii:
    def test_occupancy_note_shown_in_full_for_private_audience(self, session_factory) -> None:
        with session_factory() as session:
            _seed(session, occupancy_note="占用人王小明拒絕遷讓")
        service = AuctionCommandService(session_factory)
        response = service.risk("桃園地方法院", "115XXX", audience="private")
        assert "占用情況：占用人王小明拒絕遷讓" in response.text

    def test_occupancy_note_omitted_for_public_audience_chinese_name(self, session_factory) -> None:
        with session_factory() as session:
            _seed(session, occupancy_note="占用人王小明拒絕遷讓")
        service = AuctionCommandService(session_factory)
        response = service.risk("桃園地方法院", "115XXX", audience="public")
        assert "王小明" not in response.text
        assert "占用情況" not in response.text

    def test_occupancy_note_omitted_for_public_audience_english_name(self, session_factory) -> None:
        with session_factory() as session:
            _seed(session, occupancy_note="Occupant John Smith refuses to vacate the premises")
        service = AuctionCommandService(session_factory)
        response = service.risk("桃園地方法院", "115XXX", audience="public")
        assert "John Smith" not in response.text
        assert "占用情況" not in response.text

    def test_risk_default_audience_is_public_safe(self, session_factory) -> None:
        with session_factory() as session:
            _seed(session, occupancy_note="占用人王小明拒絕遷讓")
        service = AuctionCommandService(session_factory)
        response = service.risk("桃園地方法院", "115XXX")  # no audience passed
        assert response.audience == "public"
        assert "王小明" not in response.text


class TestDetailCommandPii:
    def test_occupancy_note_shown_for_private_hidden_for_public(self, session_factory) -> None:
        with session_factory() as session:
            session.add(
                MarketPrice(
                    city="桃園市",
                    district="中壢區",
                    building_type="住宅",
                    average_unit_price_twd=358_000,
                    transaction_count=40,
                )
            )
            _seed(session, occupancy_note="占用人 John Smith 與 王小明 拒絕遷讓")
        service = AuctionCommandService(session_factory)
        private = service.detail("桃園地方法院", "115XXX", audience="private")
        public = service.detail("桃園地方法院", "115XXX", audience="public")
        assert "John Smith" in private.text and "王小明" in private.text
        assert "John Smith" not in public.text and "王小明" not in public.text


class TestAuctionCommandServiceBasics:
    def test_search_and_latest(self, session_factory) -> None:
        with session_factory() as session:
            _seed(session)
        service = AuctionCommandService(session_factory)
        found = service.search(AuctionSearchInput(city="桃園市"))
        assert "115XXX" in found.text
        latest = service.latest()
        assert "115XXX" in latest.text

    def test_subscribe_and_unsubscribe(self, session_factory) -> None:
        service = AuctionCommandService(session_factory)
        sub = service.subscribe(999, AuctionSearchInput(city="桃園市"))
        assert sub.audience == "private"
        assert "已建立法拍案件訂閱" in sub.text
        unsub = service.unsubscribe(999, 1)
        assert "已取消訂閱" in unsub.text

    def test_detail_and_risk_unknown_case(self, session_factory) -> None:
        service = AuctionCommandService(session_factory)
        assert "查無案件" in service.detail("no", "such-case").text
        assert "查無案件" in service.risk("no", "such-case").text
