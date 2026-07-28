from __future__ import annotations

from crawlers.sale.base import SaleCrawler, SaleListing


class CompositeSaleCrawler(SaleCrawler):
    def __init__(self, sources: dict[str, SaleCrawler]) -> None:
        super().__init__()
        if not sources:
            raise ValueError("at least one sale source is required")
        self.sources = sources
        self.last_failures: dict[str, str] = {}

    async def fetch(self) -> list[SaleListing]:
        combined: dict[tuple[str, str], SaleListing] = {}
        self.last_failures = {}
        for name, source in self.sources.items():
            try:
                items = await source.fetch()
            except Exception as error:
                self.last_failures[name] = str(error)[:1000]
                continue
            for item in items:
                combined[(item.source, item.source_property_id)] = item
        if not combined:
            failures = "; ".join(
                f"{name}: {error}" for name, error in self.last_failures.items()
            )
            raise RuntimeError(f"all sale sources failed: {failures}")
        return list(combined.values())
