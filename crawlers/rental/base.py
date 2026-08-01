from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from crawlers.sale.base import CompliancePolicy


@dataclass(frozen=True)
class RentalListing:
    source: str
    source_property_id: str
    url: str
    title: str
    city: str
    district: str
    monthly_rent_twd: int
    rent_per_ping_twd: int
    area_ping: Decimal
    address: str | None = None
    layout: str | None = None
    floor: str | None = None
    total_floors: int | None = None
    rental_type: str | None = None
    landlord_type: str | None = None
    features: str | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if self.monthly_rent_twd <= 0 or self.rent_per_ping_twd <= 0:
            raise ValueError("rental prices must be positive")
        if self.area_ping <= 0:
            raise ValueError("rental area must be positive")

    def to_model_values(self) -> dict[str, Any]:
        return asdict(self)


class RentalCrawler(ABC):
    def __init__(self, policy: CompliancePolicy | None = None) -> None:
        self.policy = policy or CompliancePolicy()

    @abstractmethod
    async def fetch(self) -> list[RentalListing]:
        raise NotImplementedError
