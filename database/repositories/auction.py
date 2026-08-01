"""Repository for auction cases and subscriptions.

Mirrors ``database.repositories.sale``'s ``PropertyRepository`` /
``SubscriptionRepository`` shape: a plain class wrapping a SQLAlchemy
``Session``, callable against SQLite (tests) or PostgreSQL (prod) --
this is the real, persistent implementation, not the in-memory
placeholder a standalone auction prototype used before this vertical
was ported into the shared database layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session, selectinload

from database.models.auction import (
    ACTIVE_STATUSES,
    AuctionCase,
    AuctionSubscription,
    CaseType,
    OccupancyStatus,
)

_EAGER_LOAD = (
    selectinload(AuctionCase.rounds),
    selectinload(AuctionCase.documents),
    selectinload(AuctionCase.status_history),
)


@dataclass(frozen=True)
class AuctionSearchFilters:
    """Shared predicate shape for ``/auction search`` and subscription matching.

    Kept as one type (mirroring ``database.repositories.sale.PropertySearch``)
    so search and subscription-creation can never drift apart -- spec
    section 六's search example and the /auction command list both
    describe the same filterable attributes.
    """

    city: str | None = None
    district: str | None = None
    case_type: CaseType | None = None
    max_floor_price_twd: int | None = None  # 底價上限
    min_round: int | None = None  # 拍次: 二拍以上 -> min_round=2
    require_deliverable: bool | None = None  # 點交：是/否 (None = 不限)
    min_investment_score: Decimal | None = None
    limit: int = 20


class AuctionRepository:
    def __init__(self, session: Session):
        self.session = session

    def add(self, case: AuctionCase) -> AuctionCase:
        self.session.add(case)
        self.session.flush()
        return case

    def get(self, case_id: int) -> AuctionCase | None:
        return self.session.scalar(select(AuctionCase).options(*_EAGER_LOAD).where(AuctionCase.id == case_id))

    def get_by_case_number(self, court_name: str, case_number: str) -> AuctionCase | None:
        return self.session.scalar(
            select(AuctionCase)
            .options(*_EAGER_LOAD)
            .where(AuctionCase.court_name == court_name, AuctionCase.case_number == case_number)
        )

    def search(self, filters: AuctionSearchFilters) -> list[AuctionCase]:
        stmt: Select[tuple[AuctionCase]] = select(AuctionCase).options(*_EAGER_LOAD)
        if filters.city:
            stmt = stmt.where(AuctionCase.city == filters.city)
        if filters.district:
            stmt = stmt.where(AuctionCase.district == filters.district)
        if filters.case_type:
            stmt = stmt.where(AuctionCase.case_type == filters.case_type)
        if filters.require_deliverable is not None:
            wanted = (
                (OccupancyStatus.VACANT_DELIVERABLE, OccupancyStatus.OCCUPIED_DELIVERABLE)
                if filters.require_deliverable
                else (OccupancyStatus.NOT_DELIVERABLE,)
            )
            stmt = stmt.where(AuctionCase.occupancy_status.in_(wanted))
        if filters.min_investment_score is not None:
            stmt = stmt.where(AuctionCase.investment_score >= filters.min_investment_score)
        cases = list(self.session.scalars(stmt.order_by(desc(AuctionCase.updated_at))))
        # max_floor_price_twd / min_round are properties of the CURRENT
        # round (a child row), not a column on auction_cases -- filtered
        # in-process against the already eager-loaded rounds rather than a
        # correlated subquery. Fine at v1 case volumes; revisit if this
        # ever needs to scale past an in-process filter.
        if filters.max_floor_price_twd is not None:
            cases = [
                c for c in cases if c.current_round and c.current_round.floor_price_total_twd <= filters.max_floor_price_twd
            ]
        if filters.min_round is not None:
            cases = [c for c in cases if (c.round_number or 0) >= filters.min_round]
        return cases[: filters.limit]

    def latest(self, limit: int = 10) -> list[AuctionCase]:
        return list(
            self.session.scalars(
                select(AuctionCase).options(*_EAGER_LOAD).order_by(desc(AuctionCase.first_seen_at)).limit(limit)
            )
        )

    def upcoming(self, *, within_days: int, today: date | None = None) -> list[AuctionCase]:
        """Cases whose current round is still active and auctioning within ``within_days``.

        Excludes FAILED and every terminal status (SUSPENDED/WITHDRAWN/
        AWARDED) via ``ACTIVE_STATUSES`` -- a case that's already resolved
        or between rounds has no valid "still going to auction on this
        date" to report.
        """
        reference = today or datetime.now(timezone.utc).date()
        cases = list(
            self.session.scalars(
                select(AuctionCase).options(*_EAGER_LOAD).where(AuctionCase.status.in_(ACTIVE_STATUSES))
            )
        )
        upcoming: list[AuctionCase] = []
        for case in cases:
            current = case.current_round
            if current is None or current.auction_date is None:
                continue
            days_out = (current.auction_date - reference).days
            if 0 <= days_out <= within_days:
                upcoming.append(case)
        upcoming.sort(key=lambda c: c.current_round.auction_date)
        return upcoming


class AuctionSubscriptionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **values: object) -> AuctionSubscription:
        subscription = AuctionSubscription(**values)
        self.session.add(subscription)
        self.session.flush()
        return subscription

    def get(self, subscription_id: int) -> AuctionSubscription | None:
        return self.session.get(AuctionSubscription, subscription_id)

    def list_for_user(self, discord_user_id: int) -> list[AuctionSubscription]:
        return list(
            self.session.scalars(
                select(AuctionSubscription).where(
                    AuctionSubscription.discord_user_id == discord_user_id,
                    AuctionSubscription.active.is_(True),
                )
            )
        )

    def deactivate(self, discord_user_id: int, subscription_id: int) -> bool:
        item = self.session.scalar(
            select(AuctionSubscription).where(
                AuctionSubscription.id == subscription_id,
                AuctionSubscription.discord_user_id == discord_user_id,
                AuctionSubscription.active.is_(True),
            )
        )
        if item is None:
            return False
        item.active = False
        self.session.flush()
        return True
