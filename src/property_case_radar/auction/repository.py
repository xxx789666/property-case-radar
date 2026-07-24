"""Storage contract for auction cases and subscriptions.

DB INTEGRATION POINT: this is the second half of the "minimal compatible
model" called for when the shared database layer does not exist yet (see
models.py's module docstring). ``AuctionRepository`` is the interface
discord/commands.py programs against. ``InMemoryAuctionRepository`` is a
plain-list implementation used by this vertical's tests and safe as a
throwaway store for local manual testing; it is NOT suitable for
production (no persistence, no concurrency control).

When a PostgreSQL-backed repository lands, it only needs to satisfy this
Protocol -- commands.py, notifications.py, and scoring.py do not need to
change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from property_case_radar.auction.models import AuctionCase, AuctionSubscription


@runtime_checkable
class AuctionRepository(Protocol):
    def add_case(self, case: AuctionCase) -> None: ...

    def get_case(self, case_id: str) -> AuctionCase | None: ...

    def get_case_by_number(self, court_name: str, case_number: str) -> AuctionCase | None: ...

    def list_cases(self) -> list[AuctionCase]: ...

    def add_subscription(self, subscription: AuctionSubscription) -> None: ...

    def get_subscription(self, subscription_id: str) -> AuctionSubscription | None: ...

    def list_subscriptions(self, user_id: str) -> list[AuctionSubscription]: ...

    def remove_subscription(self, subscription_id: str) -> bool: ...


class InMemoryAuctionRepository:
    """Reference implementation of AuctionRepository, backed by plain lists."""

    def __init__(self) -> None:
        self._cases: dict[str, AuctionCase] = {}
        self._subscriptions: dict[str, AuctionSubscription] = {}

    def add_case(self, case: AuctionCase) -> None:
        self._cases[case.case_id] = case

    def get_case(self, case_id: str) -> AuctionCase | None:
        return self._cases.get(case_id)

    def get_case_by_number(self, court_name: str, case_number: str) -> AuctionCase | None:
        for case in self._cases.values():
            if case.court_name == court_name and case.case_number == case_number:
                return case
        return None

    def list_cases(self) -> list[AuctionCase]:
        return list(self._cases.values())

    def add_subscription(self, subscription: AuctionSubscription) -> None:
        self._subscriptions[subscription.subscription_id] = subscription

    def get_subscription(self, subscription_id: str) -> AuctionSubscription | None:
        return self._subscriptions.get(subscription_id)

    def list_subscriptions(self, user_id: str) -> list[AuctionSubscription]:
        return [s for s in self._subscriptions.values() if s.user_id == user_id and s.active]

    def remove_subscription(self, subscription_id: str) -> bool:
        sub = self._subscriptions.get(subscription_id)
        if sub is None:
            return False
        sub.active = False
        return True
