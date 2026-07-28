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
import re
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
MOI_HISTORY_SEASONS_URL = (
    "https://plvr.land.moi.gov.tw/DownloadSeason_ajax_list"
)
MOI_HISTORY_ZIP_URL = (
    "https://plvr.land.moi.gov.tw/DownloadSeason"
    "?season={season}&type=zip&fileName=lvr_landcsv.zip"
)
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


def classify_land_subtype(
    *,
    urban_zoning: str = "",
    non_urban_zone: str = "",
    non_urban_use: str = "",
) -> str:
    """Classify official land comparables without mixing incompatible uses."""
    designated = non_urban_use or ""
    if "丁種建築用地" in designated:
        return "土地:工業用地"
    if "農牧用地" in designated:
        return "土地:農地"
    if any(
        marker in designated
        for marker in ("甲種建築用地", "乙種建築用地", "丙種建築用地")
    ):
        return "土地:建地"
    urban = urban_zoning or ""
    if "工業" in urban:
        return "土地:工業用地"
    if any(marker in urban for marker in ("住宅區", "商業區")):
        return "土地:建地"
    non_urban = non_urban_zone or ""
    if "工業區" in non_urban:
        return "土地:工業用地"
    if any(marker in non_urban for marker in ("農業區", "特定農業區", "一般農業區")):
        return "土地:農地"
    return "土地:其他"


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
    oldest_date: date | None = None,
    only_land: bool = False,
) -> tuple[list[ActualTransactionRecord], int]:
    """Parse the official current sales ZIP into bounded, non-PII summaries.

    Normal building transactions and land-only transactions with positive
    prices are retained.  Land-only rows use their official land unit price
    and derive area from total/unit price when the compact fixture/schema has
    no dedicated land-area field. Parking-only rows remain excluded.
    """

    reference_date = today or date.today()
    oldest = oldest_date or reference_date - timedelta(days=max_age_days)
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
                        if target.startswith("transaction ") or target == "車位":
                            skipped += 1
                            continue
                        is_land_only = (
                            "土地" in target
                            and "建物" not in target
                            and "房地" not in target
                        )
                        if only_land and not is_land_only:
                            continue
                        if (
                            not is_land_only
                            and "建物" not in target
                            and "房地" not in target
                        ):
                            skipped += 1
                            continue
                        transaction_date = parse_roc_date(row["交易年月日"])
                        if transaction_date < oldest or transaction_date > reference_date + timedelta(days=1):
                            skipped += 1
                            continue
                        total_price = int(row["總價元"])
                        unit_price_sqm = int(row["單價元平方公尺"])
                        building_area_sqm = (
                            total_price / unit_price_sqm
                            if is_land_only and unit_price_sqm > 0
                            else float(row["建物移轉總面積平方公尺"])
                        )
                        if min(total_price, unit_price_sqm) <= 0 or building_area_sqm <= 0:
                            skipped += 1
                            continue
                        building_type = normalize_building_type(
                            row["建物型態"], target
                        )
                        if building_type == "土地":
                            building_type = classify_land_subtype(
                                urban_zoning=row.get("都市土地使用分區", ""),
                                non_urban_zone=row.get("非都市土地使用分區", ""),
                                non_urban_use=row.get("非都市土地使用編定", ""),
                            )
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
                                building_type=building_type,
                                source_id=(row.get("編號") or "").strip() or None,
                            )
                        )
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    if len(records) > max_records:
                        raise MoiOpenDataError("MOI current batch exceeds the configured record bound")
    return records, skipped


def parse_available_seasons(html_text: str) -> list[str]:
    seasons = set(re.findall(r'value=["\'](\d{3}S[1-4])["\']', html_text))
    return sorted(
        seasons,
        key=lambda value: (int(value[:3]), int(value[-1])),
        reverse=True,
    )


def _years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _transaction_key(record: ActualTransactionRecord) -> tuple[object, ...]:
    if record.source_id:
        return (record.city, record.source_id)
    return (
        record.city,
        record.district,
        record.transaction_date,
        record.unit_price_per_ping_twd,
        record.total_price_twd,
        round(record.building_area_ping, 4),
        record.building_type,
    )


class MoiActualPriceSource:
    """Bounded HTTP client for current and recent official MOI sales data."""

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
        history_years: int = 3,
        history_seasons_url: str = MOI_HISTORY_SEASONS_URL,
        history_zip_url: str = MOI_HISTORY_ZIP_URL,
        history_max_bytes: int = 32 * 1024 * 1024,
        today: date | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if min_request_interval_seconds < 1:
            raise ValueError("official-source request interval must be at least one second")
        if not 0 <= history_years <= 10:
            raise ValueError("history_years must be between 0 and 10")
        self.cache_dir = Path(cache_dir)
        self.url = url
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.min_request_interval_seconds = min_request_interval_seconds
        self.history_years = history_years
        self.history_seasons_url = history_seasons_url
        self.history_zip_url = history_zip_url
        self.history_max_bytes = history_max_bytes
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

    def _history_archive_path(self, season: str) -> Path:
        return self.cache_dir / f"moi-history-{season}.zip"

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_request_interval_seconds:
                await asyncio.sleep(self.min_request_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise MoiOpenDataError(
                        "MOI download failed after bounded retries"
                    ) from exc
                await asyncio.sleep(max(1, 2**attempt))
                continue
            if (
                response.status_code in {429, 500, 502, 503, 504}
                and attempt < self.max_retries
            ):
                await asyncio.sleep(max(1, 2**attempt))
                continue
            return response
        raise MoiOpenDataError("MOI download produced no response")

    async def _fetch_land_history(
        self,
        client: httpx.AsyncClient,
        *,
        reference_date: date,
    ) -> tuple[list[ActualTransactionRecord], int, bool]:
        if self.history_years == 0:
            return [], 0, False

        response = await self._request(
            client,
            self.history_seasons_url,
            headers={"User-Agent": self.user_agent, "Accept": "text/html"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MoiOpenDataError(
                f"MOI season list returned HTTP {response.status_code}"
            ) from exc
        if len(response.content) > 1024 * 1024:
            raise MoiOpenDataError("MOI season list exceeds configured size bound")
        available = parse_available_seasons(response.text)
        requested_count = self.history_years * 4
        seasons = available[:requested_count]
        if len(seasons) < requested_count:
            raise MoiOpenDataError(
                f"MOI published only {len(seasons)} of {requested_count} required seasons"
            )

        oldest = _years_ago(reference_date, self.history_years)
        records: list[ActualTransactionRecord] = []
        skipped = 0
        downloaded = False
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for season in seasons:
            archive_path = self._history_archive_path(season)
            archive_downloaded = False
            try:
                content = archive_path.read_bytes()
            except OSError:
                season_response = await self._request(
                    client,
                    self.history_zip_url.format(season=season),
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/zip",
                    },
                )
                try:
                    season_response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise MoiOpenDataError(
                        f"MOI season {season} returned HTTP "
                        f"{season_response.status_code}"
                    ) from exc
                content = season_response.content
                if len(content) > self.history_max_bytes:
                    raise MoiOpenDataError(
                        f"MOI season {season} exceeds configured size bound"
                    )
                archive_downloaded = True
            season_records, season_skipped = parse_moi_zip(
                content,
                today=reference_date,
                oldest_date=oldest,
                max_records=150_000,
                only_land=True,
            )
            if archive_downloaded:
                archive_tmp = archive_path.with_suffix(".zip.tmp")
                archive_tmp.write_bytes(content)
                os.replace(archive_tmp, archive_path)
                downloaded = True
            records.extend(season_records)
            skipped += season_skipped
        return records, skipped, downloaded

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
            response = await self._request(client, self.url, headers=headers)
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

            reference_date = self.today or date.today()
            current_records, skipped = parse_moi_zip(
                content,
                today=reference_date,
                oldest_date=_years_ago(reference_date, self.history_years or 1),
            )
            history_records, history_skipped, history_downloaded = (
                await self._fetch_land_history(
                    client,
                    reference_date=reference_date,
                )
            )
            unique_records: dict[tuple[object, ...], ActualTransactionRecord] = {}
            for record in current_records + history_records:
                unique_records.setdefault(_transaction_key(record), record)
            return MoiBatch(
                records=list(unique_records.values()),
                downloaded=downloaded or history_downloaded,
                etag=response.headers.get("etag") or metadata.get("etag"),
                last_modified=response.headers.get("last-modified") or metadata.get("last_modified"),
                skipped_rows=skipped + history_skipped,
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
        if (record.building_type or "").startswith("土地:"):
            # Auctions often have no reliable zoning, so keep the broad land
            # aggregate for them. Sale listings never use this broad key.
            groups[(record.city, record.district, "土地")].append(record)

    current_group_keys = set(groups)
    stale_land_segments = session.scalars(
        select(MarketPrice).where(MarketPrice.building_type.like("土地:%"))
    ).all()
    for item in stale_land_segments:
        if (item.city, item.district, item.building_type) not in current_group_keys:
            session.delete(item)

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
