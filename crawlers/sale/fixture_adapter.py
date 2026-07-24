import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from crawlers.sale.base import CompliancePolicy, SaleCrawler, SaleListing


class FixtureSaleCrawler(SaleCrawler):
    """Offline adapter used for deterministic development and tests."""

    def __init__(self, fixture_path: str | Path, policy: CompliancePolicy | None = None):
        super().__init__(policy)
        self.fixture_path = Path(fixture_path)

    async def fetch(self) -> list[SaleListing]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        return [self._parse(item) for item in payload]

    @staticmethod
    def _parse(item: dict[str, object]) -> SaleListing:
        values = dict(item)
        for field in ("building_area_ping", "land_area_ping", "age_years", "discount_rate"):
            if values.get(field) is not None:
                values[field] = Decimal(str(values[field]))
        if values.get("listed_date"):
            values["listed_date"] = date.fromisoformat(str(values["listed_date"]))
        return SaleListing(**values)
