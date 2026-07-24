from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CompliancePolicy:
    public_pages_only: bool = True
    bypass_login: bool = False
    bypass_anti_bot: bool = False
    min_delay_seconds: float = 2
    max_delay_seconds: float = 5
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.bypass_login or self.bypass_anti_bot:
            raise ValueError("login and anti-bot bypass are prohibited")
        if self.min_delay_seconds < 0 or self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("invalid crawler delay range")


@dataclass(frozen=True)
class SaleListing:
    source: str
    source_property_id: str
    url: str
    city: str
    district: str
    total_price_twd: int
    unit_price_per_ping_twd: int
    building_area_ping: Decimal
    address: str | None = None
    land_area_ping: Decimal | None = None
    age_years: Decimal | None = None
    floor: str | None = None
    total_floors: int | None = None
    layout: str | None = None
    building_type: str | None = None
    usage: str | None = None
    has_parking: bool | None = None
    listed_date: date | None = None
    market_unit_price_twd: int | None = None
    discount_rate: Decimal | None = None
    score: int | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if self.total_price_twd <= 0 or self.unit_price_per_ping_twd <= 0:
            raise ValueError("listing prices must be positive")
        if self.building_area_ping <= 0:
            raise ValueError("building area must be positive")

    def to_property_values(self) -> dict[str, Any]:
        return asdict(self)


class SaleCrawler(ABC):
    """Contract for approved/public sale sources.

    Implementations must honor the policy and must not bypass authentication,
    paywalls, CAPTCHAs, rate limits, or other access controls.
    """

    def __init__(self, policy: CompliancePolicy | None = None):
        self.policy = policy or CompliancePolicy()

    @abstractmethod
    async def fetch(self) -> list[SaleListing]:
        raise NotImplementedError
