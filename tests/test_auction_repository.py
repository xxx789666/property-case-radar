from datetime import date, datetime, timezone
from decimal import Decimal

from database.models.auction import (
    AuctionCase,
    AuctionRound,
    CaseType,
    OccupancyStatus,
)
from database.repositories.auction import (
    AuctionRepository,
    AuctionSearchFilters,
    AuctionSubscriptionRepository,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _seed(session, **overrides) -> AuctionCase:
    defaults = dict(
        court_name="桃園地方法院",
        case_number="115XXX",
        city="桃園市",
        district="中壢區",
        case_type=CaseType.RESIDENTIAL,
        occupancy_status=OccupancyStatus.VACANT_DELIVERABLE,
        first_seen_at=utc(2026, 7, 1),
        updated_at=utc(2026, 7, 1),
        investment_score=Decimal("88"),
        rounds=[
            AuctionRound(
                round_number=2, floor_price_total_twd=9_800_000, floor_unit_price_twd=232_000, auction_date=date(2026, 8, 18)
            )
        ],
    )
    defaults.update(overrides)
    case = AuctionCase(**defaults)
    session.add(case)
    session.commit()
    return case


def test_add_and_get_by_case_number(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        found = AuctionRepository(session).get_by_case_number("桃園地方法院", "115XXX")
        assert found is not None
        assert found.round_number == 2
        assert found.current_round.floor_price_total_twd == 9_800_000


def test_get_by_case_number_missing_returns_none(session_factory) -> None:
    with session_factory() as session:
        assert AuctionRepository(session).get_by_case_number("nope", "nope") is None


def test_search_filters_city_district(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        _seed(session, court_name="台北地方法院", case_number="116YYY", city="台北市", district="大安區")
        session.commit()
        repo = AuctionRepository(session)
        results = repo.search(AuctionSearchFilters(city="桃園市"))
        assert [c.case_number for c in results] == ["115XXX"]


def test_search_filters_max_floor_price(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        repo = AuctionRepository(session)
        assert len(repo.search(AuctionSearchFilters(max_floor_price_twd=10_000_000))) == 1
        assert len(repo.search(AuctionSearchFilters(max_floor_price_twd=1_000_000))) == 0


def test_search_filters_min_round(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        repo = AuctionRepository(session)
        assert len(repo.search(AuctionSearchFilters(min_round=2))) == 1
        assert len(repo.search(AuctionSearchFilters(min_round=3))) == 0


def test_search_filters_require_deliverable(session_factory) -> None:
    with session_factory() as session:
        _seed(session, occupancy_status=OccupancyStatus.NOT_DELIVERABLE)
        repo = AuctionRepository(session)
        assert len(repo.search(AuctionSearchFilters(require_deliverable=True))) == 0
        assert len(repo.search(AuctionSearchFilters(require_deliverable=False))) == 1


def test_search_filters_min_investment_score(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        repo = AuctionRepository(session)
        assert len(repo.search(AuctionSearchFilters(min_investment_score=Decimal("50")))) == 1
        assert len(repo.search(AuctionSearchFilters(min_investment_score=Decimal("95")))) == 0


def test_latest_orders_by_first_seen_at_desc(session_factory) -> None:
    with session_factory() as session:
        _seed(session, case_number="115OLD", first_seen_at=utc(2026, 1, 1))
        _seed(session, court_name="c2", case_number="115NEW", first_seen_at=utc(2026, 7, 1))
        repo = AuctionRepository(session)
        latest = repo.latest(limit=1)
        assert latest[0].case_number == "115NEW"


def test_upcoming_excludes_terminal_and_failed_status(session_factory) -> None:
    from database.models.auction import AuctionStatus

    with session_factory() as session:
        case_active = _seed(session, case_number="ACTIVE")
        case_awarded = _seed(session, court_name="c2", case_number="AWARDED", first_seen_at=utc(2026, 7, 2))
        case_awarded.status = AuctionStatus.AWARDED
        session.commit()
        repo = AuctionRepository(session)
        upcoming = repo.upcoming(within_days=60, today=date(2026, 7, 24))
        assert [c.case_number for c in upcoming] == ["ACTIVE"]


def test_upcoming_respects_window(session_factory) -> None:
    with session_factory() as session:
        _seed(session)
        repo = AuctionRepository(session)
        assert len(repo.upcoming(within_days=1, today=date(2026, 7, 24))) == 0
        assert len(repo.upcoming(within_days=60, today=date(2026, 7, 24))) == 1


def test_subscription_crud(session_factory) -> None:
    with session_factory() as session:
        repo = AuctionSubscriptionRepository(session)
        sub = repo.create(discord_user_id=42, city="桃園市", min_round=2)
        session.commit()
        assert repo.get(sub.id).discord_user_id == 42
        assert [s.id for s in repo.list_for_user(42)] == [sub.id]

        assert repo.deactivate(999, sub.id) is False  # wrong user
        assert repo.deactivate(42, sub.id) is True
        session.commit()
        assert repo.list_for_user(42) == []
        assert repo.deactivate(42, sub.id) is False  # already inactive
