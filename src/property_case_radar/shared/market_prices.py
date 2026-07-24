"""Minimal, DB-agnostic contract for the shared ``market_prices`` table.

INTEGRATION POINT: taiwan_real_estate_radar.md section 十一 defines
``market_prices`` as a table shared by the sale and auction pipelines,
fed by 內政部實價登錄 (Ministry of Interior actual transaction price
data). No shared database/repository layer exists in this repository
yet (neither pipeline has landed a database module). Rather than block
on that, this module defines the smallest read contract the auction
scoring/search code needs:

    RegionalMarketPrice   - one region's current average transaction
                             unit price (what auction_score.py and the
                             /auction search|detail commands consume)
    MarketPriceProvider   - a Protocol any real repository can satisfy
    InMemoryMarketPriceProvider - a trivial in-memory implementation,
                             used by this vertical's tests and safe as
                             a placeholder for local/dev usage

When the real shared database layer lands (SQLAlchemy models +
PostgreSQL, per CLAUDE.md's planned ``database/`` module), it should
provide a class satisfying ``MarketPriceProvider`` and this module's
in-memory implementation can be dropped or kept for tests. Nothing in
``auction/`` should import a concrete DB/ORM type directly -- only this
Protocol -- so swapping the backing store later does not ripple through
scoring, search, or notification code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RegionalMarketPrice:
    """A region's average actual-transaction unit price (元/坪 or per spec's 萬/坪 convention).

    Fields intentionally mirror what taiwan_real_estate_radar.md calls
    "附近實價登錄均價" / "區域成交單價" -- the value both pipelines diff
    their own listing/floor price against.
    """

    city: str
    district: str
    avg_unit_price_per_ping: float  # 萬元／坪
    sample_size: int
    as_of: date


@runtime_checkable
class MarketPriceProvider(Protocol):
    """Read-only contract for looking up regional market prices.

    Any concrete implementation (in-memory, PostgreSQL-backed
    repository, HTTP client to a shared API service, ...) just needs to
    satisfy this method signature.
    """

    def get_regional_average(self, city: str, district: str) -> RegionalMarketPrice | None:
        """Return the latest known average unit price for a district, or None if unknown."""
        ...


class InMemoryMarketPriceProvider:
    """Simple in-memory ``MarketPriceProvider`` for tests and local/dev use."""

    def __init__(self, prices: list[RegionalMarketPrice] | None = None) -> None:
        self._by_key: dict[tuple[str, str], RegionalMarketPrice] = {}
        for price in prices or []:
            self.upsert(price)

    def upsert(self, price: RegionalMarketPrice) -> None:
        self._by_key[(price.city, price.district)] = price

    def get_regional_average(self, city: str, district: str) -> RegionalMarketPrice | None:
        return self._by_key.get((city, district))
