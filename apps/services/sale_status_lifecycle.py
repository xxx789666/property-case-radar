"""Reconcile stale sale listings against their public source detail pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.sale.captured_status import ListingStatusCheck
from database.models.base import utcnow
from database.models.sale import Property


class SaleStatusVerifier(Protocol):
    async def verify(
        self,
        items: list[ListingStatusCheck],
    ) -> dict[int, str]: ...


@dataclass(frozen=True)
class SaleStatusResult:
    candidates: int = 0
    confirmed_active: int = 0
    marked_inactive: int = 0
    unknown: int = 0


async def reconcile_stale_sale_listings(
    session: Session,
    verifier: SaleStatusVerifier,
    *,
    missing_days: int = 3,
    limit: int = 500,
    now: datetime | None = None,
) -> SaleStatusResult:
    checked_at = now or utcnow()
    cutoff = checked_at - timedelta(days=missing_days)
    candidates = list(
        session.scalars(
            select(Property)
            .where(
                Property.source == "591",
                Property.status == "active",
                Property.last_seen_at < cutoff,
            )
            .order_by(Property.last_seen_at, Property.id)
            .limit(limit)
        )
    )
    if not candidates:
        return SaleStatusResult()

    statuses = await verifier.verify(
        [
            ListingStatusCheck(
                id=item.id,
                source_property_id=item.source_property_id,
                url=item.url,
            )
            for item in candidates
        ]
    )
    confirmed_active = 0
    marked_inactive = 0
    unknown = 0
    for item in candidates:
        status = statuses.get(item.id, "unknown")
        if status == "active":
            # A successful detail-page check is a fresh positive sighting.
            item.last_seen_at = checked_at
            confirmed_active += 1
        elif status == "inactive":
            # Keep the history; search only returns status=active.
            item.status = "inactive"
            marked_inactive += 1
        else:
            # Network errors, CAPTCHA, or unrecognized pages never change
            # listing state.
            unknown += 1
    session.commit()
    return SaleStatusResult(
        candidates=len(candidates),
        confirmed_active=confirmed_active,
        marked_inactive=marked_inactive,
        unknown=unknown,
    )
