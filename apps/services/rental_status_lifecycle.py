"""Confirm stale rental listings against their public detail pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.rental.captured_status import RentalStatusCheck
from database.models.base import utcnow
from database.models.rental import RentalProperty


class RentalStatusVerifier(Protocol):
    async def verify(
        self, items: list[RentalStatusCheck]
    ) -> dict[int, str]: ...


@dataclass(frozen=True)
class RentalStatusResult:
    candidates: int = 0
    confirmed_active: int = 0
    marked_inactive: int = 0
    unknown: int = 0


async def reconcile_stale_rental_listings(
    session: Session,
    verifier: RentalStatusVerifier,
    *,
    missing_days: int = 3,
    limit: int = 500,
    now: datetime | None = None,
    source: str = "591-rent",
) -> RentalStatusResult:
    checked_at = now or utcnow()
    cutoff = checked_at - timedelta(days=missing_days)
    candidates = list(
        session.scalars(
            select(RentalProperty)
            .where(
                RentalProperty.source == source,
                RentalProperty.status == "active",
                RentalProperty.last_seen_at < cutoff,
            )
            .order_by(RentalProperty.last_seen_at, RentalProperty.id)
            .limit(limit)
        )
    )
    if not candidates:
        return RentalStatusResult()
    statuses = await verifier.verify(
        [
            RentalStatusCheck(
                id=item.id,
                source_property_id=item.source_property_id,
                url=item.url,
            )
            for item in candidates
        ]
    )
    confirmed_active = marked_inactive = unknown = 0
    for item in candidates:
        status = statuses.get(item.id, "unknown")
        if status == "active":
            item.last_seen_at = checked_at
            confirmed_active += 1
        elif status == "inactive":
            item.status = "inactive"
            marked_inactive += 1
        else:
            unknown += 1
    session.commit()
    return RentalStatusResult(
        candidates=len(candidates),
        confirmed_active=confirmed_active,
        marked_inactive=marked_inactive,
        unknown=unknown,
    )
