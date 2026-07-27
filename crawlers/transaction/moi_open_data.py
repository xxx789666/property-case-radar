"""Official MOI actual-price Open Data adapter.

The Ministry of the Interior publishes the current nationwide real-estate
transaction batch as a ZIP file.  This adapter downloads that public artifact
directly, identifies itself, performs at most one request per second (including
retries), honours ETag/Last-Modified validators, and parses only the bounded
current batch.  It never uses an interactive search page, account, CAPTCHA, or
private realtor source.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import ssl
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean

import httpx
import truststore
from sqlalchemy import select
from sqlalchemy.orm import Session

from crawlers.transaction.actual_price import ActualTransactionRecord
from database.models.common import MarketPrice

MOI_CURRENT_SALES_CSV_URL = "https://plvr.land.moi.gov.tw/opendata/lvr_landAcsv.zip"
DEFAULT_USER_AGENT = (
    "PropertyCaseRadar/1.0 "
    "(+https://github.com/xxx789666/Property-Case-Radar-Git; compliance contact via repository)"
)
SQUARE_METRES_PER_PING = 3.305785

_CITY_BY_FILE_PREFIX = {
    "a": "臺北市",
    "b": "臺中市",
    "c": "基隆市",
    "d": "臺南市",
    "e": "高雄市",
    "f": "新北市",
    "g": "宜蘭縣",
    "h": "桃園市",
    "i": "嘉義市",
    "j": "新竹縣",
    "k": "苗栗縣",
    "m": "南投縣",
    "n": "彰化縣",
    "o": "新竹市",
    "p": "雲林縣",
    "q": "嘉義縣",
    "t": "屏東縣",
    "u": "花蓮縣",
    "v": "臺東縣",
    "w": "金門縣",
    "x": "澎湖縣",
    "z": "連江縣",
}


class MoiOpenDataError(RuntimeError):
    """The official artifact could not be fetched or validated safely."""


@dataclass(frozen=True)
class MoiBatch:
    records: list[ActualTransactionRecord]
    downloaded: bool
    etag: str | None
    last_modified: str | None
    skipped_rows: int


@dataclass(frozen=True)
class MarketSyncReport:
    fetched_records: int
    skipped_rows: int
    groups: int
    created: int
    updated: int
    unchanged: int
    downloaded: bool


def parse_roc_date(value: str) -> date:
    digits = value.strip()
    if len(digits) != 7 or not digits.isdigit():
        raise ValueError("transaction date must be a seven-digit ROC date")
    return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))


def normalize_building_type(value: str, transaction_target: str) -> str:
    text = f"{value} {transaction_target}"
    if "店面" in text or "店舖" in text:
        return "店面"
    if any(marker in text for marker in ("辦公", "廠房", "工廠", "廠辦")):
        return "辦公廠房"
    if "土地" in transaction_target and "建物" not in transaction_target:
        return "土地"
    return "住宅"


def _main_csv_members(archive: zipfile.ZipFile) -> list[str]:
    return sorted(
        name
        for name in archive.namelist()
        if len(name) >= 16
        and name[1:] == "_lvr_land_a.csv"
        and name[0].lower() in _CITY_BY_FILE_PREFIX
    )


def parse_moi_zip(
    content: bytes,
    *,
    today: date | None = None,
    max_age_days: int = 365,
    max_records: int = 100_000,
) -> tuple[list[ActualTransactionRecord], int]:
    """Parse the official current sales ZIP into bounded, non-PII summaries.

    Only normal property transactions with a positive building area and unit
    price are retained.  Land-only and parking-only rows are intentionally
    excluded because they are not comparable to the residential/commercial
    floor-unit prices used by scoring.
    """

    reference_date = today or date.today()
    oldest = reference_date - timedelta(days=max_age_days)
    records: list[ActualTransactionRecord] = []
    skipped = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise MoiOpenDataError("MOI response is not a valid ZIP archive") from exc

    with archive:
        members = _main_csv_members(archive)
        if not members:
            raise MoiOpenDataError("MOI ZIP contains no recognized sales CSV files")
        for member in members:
            city = _CITY_BY_FILE_PREFIX[member[0].lower()]
            with archive.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                reader = csv.DictReader(text)
                required = {
                    "鄉鎮市區",
                    "交易標的",
                    "交易年月日",
                    "總價元",
                    "單價元平方公尺",
                    "建物移轉總面積平方公尺",
                    "建物型態",
                }
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise MoiOpenDataError(f"MOI CSV schema mismatch in {member}")

                for row in reader:
                    try:
                        target = row["交易標的"].strip()
                        if target.startswith("transaction ") or target in {"土地", "車位"}:
                            skipped += 1
                            continue
                        if "建物" not in target and "房地" not in target:
                            skipped += 1
                            continue
                        transaction_date = parse_roc_date(row["交易年月日"])
                        if transaction_date < oldest or transaction_date > reference_date + timedelta(days=1):
                            skipped += 1
                            continue
                        total_price = int(row["總價元"])
                        unit_price_sqm = int(row["單價元平方公尺"])
                        building_area_sqm = float(row["建物移轉總面積平方公尺"])
                        if min(total_price, unit_price_sqm) <= 0 or building_area_sqm <= 0:
                            skipped += 1
                            continue
                        records.append(
                            ActualTransactionRecord(
                                city=city,
                                district=row["鄉鎮市區"].strip(),
                                transaction_date=transaction_date,
                                unit_price_per_ping_twd=round(unit_price_sqm * SQUARE_METRES_PER_PING),
                                total_price_twd=total_price,
                                building_area_ping=building_area_sqm / SQUARE_METRES_PER_PING,
                                # Full addresses are deliberately not retained:
                                # market_prices needs only aggregated area data.
                                address=None,
                                building_type=normalize_building_type(row["建物型態"], target),
                            )
                        )
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    if len(records) > max_records:
                        raise MoiOpenDataError("MOI current batch exceeds the configured record bound")
    return records, skipped


class MoiActualPriceSource:
    """Bounded HTTP client for the official MOI current sales batch."""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        url: str = MOI_CURRENT_SALES_CSV_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 30,
        max_bytes: int = 25 * 1024 * 1024,
        max_retries: int = 2,
        min_request_interval_seconds: float = 1,
        today: date | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if min_request_interval_seconds < 1:
            raise ValueError("official-source request interval must be at least one second")
        self.cache_dir = Path(cache_dir)
        self.url = url
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.min_request_interval_seconds = min_request_interval_seconds
        self.today = today
        self._client = client
        self._last_request_at: float | None = None

    @property
    def _archive_path(self) -> Path:
        return self.cache_dir / "moi-current-sales.zip"

    @property
    def _metadata_path(self) -> Path:
        return self.cache_dir / "moi-current-sales.json"

    def _load_metadata(self) -> dict[str, str]:
        try:
            value = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_cache(self, content: bytes, response: httpx.Response) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        archive_tmp = self._archive_path.with_suffix(".zip.tmp")
        metadata_tmp = self._metadata_path.with_suffix(".json.tmp")
        archive_tmp.write_bytes(content)
        metadata_tmp.write_text(
            json.dumps(
                {
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        os.replace(archive_tmp, self._archive_path)
        os.replace(metadata_tmp, self._metadata_path)

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_request_interval_seconds:
                await asyncio.sleep(self.min_request_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    async def fetch(self) -> MoiBatch:
        metadata = self._load_metadata()
        headers = {"User-Agent": self.user_agent, "Accept": "application/zip"}
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("last_modified"):
            headers["If-Modified-Since"] = metadata["last_modified"]

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )
        try:
            response: httpx.Response | None = None
            for attempt in range(self.max_retries + 1):
                await self._throttle()
                try:
                    response = await client.get(self.url, headers=headers)
                except httpx.HTTPError as exc:
                    if attempt >= self.max_retries:
                        raise MoiOpenDataError("MOI download failed after bounded retries") from exc
                    await asyncio.sleep(max(1, 2**attempt))
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    await asyncio.sleep(max(1, 2**attempt))
                    continue
                break

            if response is None:
                raise MoiOpenDataError("MOI download produced no response")
            if response.status_code == 304:
                try:
                    content = self._archive_path.read_bytes()
                except OSError as exc:
                    raise MoiOpenDataError("MOI returned 304 but no cached archive exists") from exc
                downloaded = False
            else:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise MoiOpenDataError(f"MOI download returned HTTP {response.status_code}") from exc
                content = response.content
                if len(content) > self.max_bytes:
                    raise MoiOpenDataError("MOI archive exceeds configured size bound")
                self._write_cache(content, response)
                downloaded = True

            records, skipped = parse_moi_zip(content, today=self.today)
            return MoiBatch(
                records=records,
                downloaded=downloaded,
                etag=response.headers.get("etag") or metadata.get("etag"),
                last_modified=response.headers.get("last-modified") or metadata.get("last_modified"),
                skipped_rows=skipped,
            )
        finally:
            if owns_client:
                await client.aclose()


async def sync_market_prices(source: MoiActualPriceSource, session: Session) -> MarketSyncReport:
    """Aggregate the current official batch and idempotently upsert market_prices."""

    batch = await source.fetch()
    groups: dict[tuple[str, str, str], list[ActualTransactionRecord]] = defaultdict(list)
    for record in batch.records:
        groups[(record.city, record.district, record.building_type or "住宅")].append(record)

    created = updated = unchanged = 0
    for (city, district, building_type), records in sorted(groups.items()):
        values = {
            "average_unit_price_twd": round(fmean(item.unit_price_per_ping_twd for item in records)),
            "transaction_count": len(records),
            "period_start": min(item.transaction_date for item in records),
            "period_end": max(item.transaction_date for item in records),
        }
        item = session.scalar(
            select(MarketPrice).where(
                MarketPrice.city == city,
                MarketPrice.district == district,
                MarketPrice.building_type == building_type,
            )
        )
        if item is None:
            session.add(MarketPrice(city=city, district=district, building_type=building_type, **values))
            created += 1
            continue
        changed = any(getattr(item, key) != value for key, value in values.items())
        if not changed:
            unchanged += 1
            continue
        for key, value in values.items():
            setattr(item, key, value)
        updated += 1
    session.commit()
    return MarketSyncReport(
        fetched_records=len(batch.records),
        skipped_rows=batch.skipped_rows,
        groups=len(groups),
        created=created,
        updated=updated,
        unchanged=unchanged,
        downloaded=batch.downloaded,
    )
