"""Read-only Property Case Radar query tool for the OpenAB / Codex ACP
Discord LLM bridge (see ``openab/sidecar/``).

This is the ONLY way the LLM agent touches Radar's data. It is invoked as a
subprocess (``python -m tools.radar_agent_query <subcommand> ...``) by Codex
inside the SIDECAR container only (per ``openab/sidecar/AGENTS.md``) --
never the gateway container, which never holds a database credential of
any kind and never runs this tool. It never imports ``discord`` (no
Discord gateway/REST access from this process at all -- the gateway
container owns the Discord connection, on a completely separate host) and
never imports the crawler, scheduler, Discord bot, ``apps.config``, or
repository modules.

Read-only is enforced at the OS/database boundary, not just by what this
module happens to call:

- Every code path here only issues ``select()`` statements against the ORM
  *models* directly (``AuctionCase``/``Property``/``MarketPrice``) -- the
  write-capable ``database.repositories`` classes (``upsert_listing``,
  ``add``, ``SubscriptionRepository.create``/``deactivate``, etc.) and the
  ``crawlers`` package they depend on are not even present in this
  container image (see ``openab/sidecar/Dockerfile.sidecar``'s COPY list
  and ``tests/test_radar_agent_query.py``'s import-surface check).
- **Level 1 (session, defense in depth)**: ``_create_read_only_engine``
  forces every actual database connection into a read-only transaction at
  the engine level, before any query runs: for PostgreSQL, every new DBAPI
  connection gets ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY``;
  for SQLite (tests/dev only) the connection is opened as a true read-only
  file (``mode=ro``) plus ``PRAGMA query_only = ON``. This alone is only a
  client-side choice, not a property of the role -- see Level 2.
- **Level 2 (role, the boundary that survives a bypass)**:
  ``_verify_role_has_no_write_privileges`` queries PostgreSQL's actual
  catalog at every invocation and fails closed if ``current_user`` is
  superuser/bypassrls/createdb/createrole, has TEMP or CREATE on the
  database, CREATE on any schema, any of
  INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on any table, or USAGE
  or UPDATE on any sequence. This is what a raw, independent connection
  using the same DSN -- bypassing this tool's Python code entirely, or one
  that explicitly issues ``SET TRANSACTION READ WRITE`` to override
  Level 1 -- still cannot get past, verified against a real PostgreSQL
  server: permanent DDL/DML, ``CREATE TEMP TABLE``, and ``nextval()`` are
  all rejected with ``psycopg.errors.InsufficientPrivilege`` and leave
  database state unchanged (see
  ``openab/sidecar/bootstrap_read_only_role.sql`` for the exact grants
  this expects, and
  ``tests/test_radar_agent_query.py::TestRealPostgresRolePrivileges``).
- ``DATABASE_URL`` is mandatory here: there is no fallback to
  ``apps.config.Settings``' hardcoded default connection string (that
  default is the *main*, write-capable application's development fallback
  and must never be reachable from this tool). The sidecar's
  ``bridge-server.mjs`` reads the ``radar_agent_database_url`` Docker
  secret directly and passes it only into each spawned ``codex-acp``
  child's own environment as ``DATABASE_URL`` -- never its own process
  environment (see that script's docstring for why).

Natural-person fields (債務人/所有權人 on auction cases) are always shown
masked (``display_debtor_owner(..., audience="public")``), the same
conservative default every other Radar surface uses when it cannot prove
its audience is a private, human-supervised context (CLAUDE.md: "所有
/auction 指令與通知函式在未指定 audience 時預設 public"). Unlike a Discord
slash command, this surface's request text ultimately comes from an LLM
whose behavior can be influenced by adversarial content inside crawled
free-text fields (e.g. ``occupancy_note``), so it deliberately does NOT
grant itself the "private search channel" exemption ``/auction detail``
uses -- that upgrade should only happen after a dedicated security review
of the agent's prompt-injection surface, not silently as a side effect of
this tool being deployed to the same channel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, desc, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, selectinload

from database.models.auction import ACTIVE_STATUSES, CASE_TYPE_LABELS, AuctionCase, CaseType
from database.models.common import MarketPrice
from database.models.sale import Property
from notifications.auction_masking import display_debtor_owner

MAX_LIMIT = 50
DATABASE_URL_ENV_VAR = "DATABASE_URL"
AUDIT_LOG_ENV_VAR = "RADAR_AGENT_QUERY_LOG"
AUCTION_DOWNLOAD_DIR_ENV_VAR = "RADAR_AUCTION_DOWNLOAD_DIR"
_ALLOWED_URL_PREFIXES = ("postgresql+psycopg://", "sqlite+pysqlite://")

_EAGER_LOAD = (selectinload(AuctionCase.rounds),)


def _original_auction_pdf_files(case: AuctionCase) -> list[str]:
    """Return only official captured PDF filenames for this case.

    The download root is operator-controlled.  User/database strings are
    treated only as path components and every candidate is resolved back
    under that root before any directory is read.  ``案件詳細資料.pdf`` and
    generated summaries are intentionally excluded: only capture-program
    files named ``<case_number>_<n>_<n>.pdf`` count as court originals.
    """

    raw_root = os.environ.get(AUCTION_DOWNLOAD_DIR_ENV_VAR, "").strip()
    if not raw_root or not re.fullmatch(r"[0-9]+", case.case_number or ""):
        return []
    root = Path(raw_root).resolve()
    candidates = []
    if case.city and case.district:
        candidates.append(root / case.city / case.district / case.case_number)
    if case.city:
        candidates.append(root / case.city / case.case_number)
    candidates.append(root / case.case_number)  # legacy capture layout
    filename_pattern = re.compile(rf"^{re.escape(case.case_number)}_[0-9]+_[0-9]+\.pdf$", re.IGNORECASE)
    found: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_dir():
            continue
        for item in resolved.iterdir():
            if item.is_file() and filename_pattern.fullmatch(item.name):
                found.add(item.name)
    return sorted(found)


class FailClosed(RuntimeError):
    """Raised for any condition where refusing to run is the only safe
    outcome -- a missing/malformed DATABASE_URL, or a database connection
    that cannot be proven read-only. Always caught at the top level and
    turned into a clean, non-zero-exit error message, never a raw
    traceback that might look like a transient/retryable failure.
    """


def _num(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def _serialize_property(item: Property) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_property_id": item.source_property_id,
        "city": item.city,
        "district": item.district,
        "address": item.address,
        "total_price_twd": item.total_price_twd,
        "unit_price_per_ping_twd": item.unit_price_per_ping_twd,
        "building_area_ping": _num(item.building_area_ping),
        "land_area_ping": _num(item.land_area_ping),
        "age_years": _num(item.age_years),
        "floor": item.floor,
        "total_floors": item.total_floors,
        "layout": item.layout,
        "building_type": item.building_type,
        "usage": item.usage,
        "has_parking": item.has_parking,
        "listed_date": item.listed_date.isoformat() if item.listed_date else None,
        "market_unit_price_twd": item.market_unit_price_twd,
        "discount_rate": _num(item.discount_rate),
        "score": item.score,
        "status": item.status,
        "source": item.source,
        "url": item.url,
    }


def _serialize_auction_case(case: AuctionCase) -> dict[str, Any]:
    current = case.current_round
    debtor, owner = display_debtor_owner(case.debtor, case.owner, audience="public")
    return {
        "id": case.id,
        "court_name": case.court_name,
        "case_number": case.case_number,
        "case_type": CASE_TYPE_LABELS.get(case.case_type, case.case_type.value),
        "city": case.city,
        "district": case.district,
        "address": case.address,
        "status": case.status.value,
        "round_number": current.round_number if current else None,
        "floor_price_total_twd": current.floor_price_total_twd if current else None,
        "floor_unit_price_twd": current.floor_unit_price_twd if current else None,
        "auction_date": current.auction_date.isoformat() if current and current.auction_date else None,
        "building_area_ping": _num(case.building_area_ping),
        "land_area_ping": _num(case.land_area_ping),
        "occupancy_status": case.occupancy_status.value,
        "ownership_type": case.ownership_type.value,
        # Masked (audience="public") -- see module docstring for why this
        # tool never applies the private-channel unmasking exemption.
        "debtor": debtor or None,
        "owner": owner or None,
        "surface_discount_rate": _num(case.surface_discount_rate),
        "risk_score": _num(case.risk_score),
        "investment_score": _num(case.investment_score),
        "announcement_url": case.announcement_url or None,
        "original_pdf_files": _original_auction_pdf_files(case),
    }


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


@dataclass(frozen=True)
class ToolError:
    error: str


def _audit_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """Return only user-facing filters, never callables or environment data."""
    return {
        key: value
        for key, value in vars(args).items()
        if key not in {"handler", "command"} and value is not None
    }


def _write_audit_log(
    args: argparse.Namespace,
    *,
    outcome: str,
    result: Any = None,
    error: str | None = None,
    elapsed_ms: int,
) -> None:
    path_value = os.environ.get(AUDIT_LOG_ENV_VAR)
    if not path_value:
        return
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "component": "radar-agent-query",
        "command": args.command,
        "arguments": _audit_arguments(args),
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
    }
    if isinstance(result, list):
        record["result_count"] = len(result)
    elif result is not None and not isinstance(result, ToolError):
        record["result_count"] = 1
    if error:
        record["error"] = error

    try:
        path = os.path.abspath(path_value)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        # A logging outage must be visible, but must not turn a valid,
        # read-only database query into a fabricated "no data" answer.
        print(f"radar_agent_query: cannot append audit log: {exc}", file=sys.stderr)


# --- read-only database boundary ----------------------------------------


def _require_database_url() -> str:
    """No default, no fallback to apps.config.Settings -- DATABASE_URL
    (set by openab/sidecar/bridge-server.mjs, which reads the
    radar_agent_database_url Docker secret file directly and places it only
    into this process's own env, per spawned codex-acp session) must be
    present and well-formed, or this refuses to run at all.
    """
    raw = os.environ.get(DATABASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise FailClosed(
            f"{DATABASE_URL_ENV_VAR} is not set. This tool never falls back to any "
            "default connection string -- set the radar_agent_database_url Docker "
            "secret (openab/.local/radar_agent_database_url) to a dedicated "
            "read-only-role connection string (see openab/README.md's 'database "
            "access' section)."
        )
    raw = raw.strip()
    if not raw.startswith(_ALLOWED_URL_PREFIXES):
        raise FailClosed(
            f"{DATABASE_URL_ENV_VAR} must start with one of {_ALLOWED_URL_PREFIXES!r} "
            f"(got a value starting with {raw.split('://', 1)[0]!r})"
        )
    try:
        make_url(raw)
    except Exception as exc:
        raise FailClosed(f"{DATABASE_URL_ENV_VAR} is not a valid database URL: {exc}") from exc
    return raw


def _create_read_only_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        # Two independent SQLite-level protections, not just Python
        # convention: the file itself is opened read-only at the OS level
        # (mode=ro -- a write attempt fails at the filesystem/VFS layer
        # regardless of any pragma), and query_only=ON additionally makes
        # SQLite's own query planner reject write statements outright.
        import sqlite3

        database = url.database or ""
        ro_uri = f"file:{database}?mode=ro"

        def _connect() -> sqlite3.Connection:
            conn = sqlite3.connect(ro_uri, uri=True)
            conn.execute("PRAGMA query_only = ON")
            return conn

        return create_engine("sqlite+pysqlite:///", creator=_connect)

    engine = create_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def _force_read_only_transaction(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        # Fires for every new physical connection (including ones opened
        # after a pool recycle/reconnect) -- this is the actual
        # enforcement, not merely documentation asking the operator to use
        # a read-only role. A role with full write GRANTs would still be
        # blocked by this at the Postgres transaction-mode level.
        #
        # SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY only takes
        # effect for the *next* transaction, not the one it is issued in --
        # psycopg opens an implicit transaction on the first statement, so
        # without the commit() below the very SET call itself runs inside
        # a still-read-write transaction, and so would the caller's first
        # real query. Committing here closes that transaction out so the
        # session-level default is what the caller's own transaction picks
        # up (verified in tests/test_radar_agent_query.py against a real
        # PostgreSQL server, not assumed from documentation).
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        finally:
            cursor.close()
        dbapi_connection.commit()

    return engine


_WRITE_TABLE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _verify_role_has_no_write_privileges(session: Session) -> None:
    """PostgreSQL only. ``_force_read_only_transaction``'s ``SET SESSION
    CHARACTERISTICS AS TRANSACTION READ ONLY`` is a *client-side choice*:
    it protects connections opened through this module, but it is not a
    property of the DSN's role -- a process that connects independently
    with the same connection string (bypassing this tool's Python code
    entirely, e.g. a raw ``psycopg.connect(...)`` from adversarial/
    prompt-injected code, or simply issuing ``SET TRANSACTION READ WRITE``
    to override the session default we set) would not inherit that
    protection. The only boundary that survives a bypass is the role's
    *actual PostgreSQL grants* -- this queries the catalog directly and
    fails closed if ``current_user`` could write anything, independent of
    whatever transaction mode is active right now.
    """
    role_row = session.execute(
        text("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")
    ).one()
    dangerous = {
        name: value
        for name, value in zip(("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole"), role_row)
        if value
    }
    if dangerous:
        raise FailClosed(
            f"current_user has dangerous role attribute(s) {sorted(dangerous)} -- refusing to query. "
            "Use a dedicated role with none of superuser/bypassrls/createdb/createrole (see "
            "openab/README.md's 'database access' section)."
        )

    # TEMP is granted to PUBLIC on every database by default -- letting
    # any role CREATE TEMPORARY TABLE, which is still a write capability
    # this "SELECT only" role must not have, even session-scoped.
    can_temp = session.execute(text("SELECT has_database_privilege(current_user, current_database(), 'TEMP')")).scalar()
    can_create_db = session.execute(
        text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
    ).scalar()
    database_violations = [name for name, value in (("TEMP", can_temp), ("CREATE", can_create_db)) if value]
    if database_violations:
        raise FailClosed(
            f"current_user has database-level privilege(s) {database_violations} -- refusing to query. "
            "Run openab/sidecar/bootstrap_read_only_role.sql's REVOKE TEMP/CREATE statements."
        )

    schemas = [
        row[0]
        for row in session.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') AND schema_name NOT LIKE 'pg\\_%'"
            )
        ).all()
    ]
    creatable = [
        schema
        for schema in schemas
        if session.execute(text("SELECT has_schema_privilege(current_user, :schema, 'CREATE')"), {"schema": schema}).scalar()
    ]
    if creatable:
        raise FailClosed(
            f"current_user has CREATE privilege on schema(s) {creatable} (could CREATE TABLE) -- refusing to query"
        )

    tables = session.execute(
        text(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE'"
        )
    ).all()
    table_violations = []
    for schema, table in tables:
        qualified = f"{schema}.{table}"
        row = session.execute(
            text(
                "SELECT "
                + ", ".join(f"has_table_privilege(current_user, :t, '{priv}')" for priv in _WRITE_TABLE_PRIVILEGES)
            ),
            {"t": qualified},
        ).one()
        granted = [priv for priv, value in zip(_WRITE_TABLE_PRIVILEGES, row) if value]
        if granted:
            table_violations.append(f"{qualified}:{','.join(granted)}")
    if table_violations:
        raise FailClosed(f"current_user has write privilege(s) on: {table_violations} -- refusing to query")

    sequences = session.execute(
        text(
            "SELECT sequence_schema, sequence_name FROM information_schema.sequences "
            "WHERE sequence_schema NOT IN ('pg_catalog', 'information_schema')"
        )
    ).all()
    # USAGE alone already permits nextval()/currval() (UPDATE additionally
    # permits setval()) -- this role has no legitimate need to read or
    # advance a sequence at all, so neither privilege is acceptable.
    sequence_violations = []
    for schema, seq in sequences:
        qualified = f"{schema}.{seq}"
        row = session.execute(
            text(
                "SELECT has_sequence_privilege(current_user, :s, 'USAGE'), "
                "has_sequence_privilege(current_user, :s, 'UPDATE')"
            ),
            {"s": qualified},
        ).one()
        granted = [priv for priv, value in zip(("USAGE", "UPDATE"), row) if value]
        if granted:
            sequence_violations.append(f"{qualified}:{','.join(granted)}")
    if sequence_violations:
        raise FailClosed(
            f"current_user has sequence privilege(s) {sequence_violations} "
            "(could call nextval/currval/setval) -- refusing to query"
        )


def _verify_read_only(session: Session, database_url: str) -> None:
    """Fail closed if, for any reason, this connection is not actually
    running in a read-only transaction -- run before any user-requested
    query executes.
    """
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        value = session.execute(text("PRAGMA query_only")).scalar()
        if value != 1:
            raise FailClosed("SQLite connection is not read-only (PRAGMA query_only != 1); refusing to query")
        return

    value = session.execute(text("SELECT current_setting('transaction_read_only')")).scalar()
    if str(value).lower() != "on":
        raise FailClosed(
            f"PostgreSQL transaction_read_only is {value!r}, not 'on', after requesting a "
            "read-only session -- refusing to query rather than risk a write-capable session"
        )

    # The check above only proves *this connection's current transaction
    # mode* -- the check below proves the *role itself* cannot write
    # regardless of transaction mode, which is what actually survives a
    # bypass of this module's own connection logic.
    _verify_role_has_no_write_privileges(session)


@contextmanager
def _read_only_session() -> Iterator[Session]:
    database_url = _require_database_url()
    engine = _create_read_only_engine(database_url)
    try:
        with Session(engine) as session:
            _verify_read_only(session, database_url)
            yield session
    finally:
        engine.dispose()


# --- query handlers (raw SELECT against models only, no repositories) ---


def _house_search(session: Session, args: argparse.Namespace) -> Any:
    stmt = select(Property).where(Property.status == "active")
    if args.city:
        stmt = stmt.where(Property.city == args.city)
    if args.district:
        stmt = stmt.where(Property.district == args.district)
    if args.max_total_price_twd is not None:
        stmt = stmt.where(Property.total_price_twd <= args.max_total_price_twd)
    if args.min_building_area_ping is not None:
        stmt = stmt.where(Property.building_area_ping >= Decimal(str(args.min_building_area_ping)))
    if args.max_age_years is not None:
        stmt = stmt.where(Property.age_years <= Decimal(str(args.max_age_years)))
    if args.min_discount_rate is not None:
        stmt = stmt.where(Property.discount_rate >= Decimal(str(args.min_discount_rate)))
    property_type = getattr(args, "property_type", None)
    if property_type == "house":
        stmt = stmt.where(Property.building_type != "土地")
    elif property_type == "land":
        stmt = stmt.where(Property.building_type == "土地")
    elif property_type == "farmland":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("農地"))
    elif property_type == "building_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("建地"))
    elif property_type == "residential_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("住宅用地"))
    elif property_type == "commercial_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("商業用地"))
    elif property_type == "industrial_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("工業用地"))
    elif property_type == "forest_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("林地"))
    elif property_type == "hillside_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("山坡地"))
    elif property_type == "road_land":
        stmt = stmt.where(Property.building_type == "土地", Property.usage.contains("道路用地"))
    stmt = stmt.order_by(
        desc(Property.score).nullslast(),
        desc(Property.first_seen_at),
    ).limit(_clamp_limit(args.limit))
    return [_serialize_property(item) for item in session.scalars(stmt)]


def _house_latest(session: Session, args: argparse.Namespace) -> Any:
    stmt = (
        select(Property)
        .where(Property.status == "active")
        .order_by(desc(Property.first_seen_at))
        .limit(_clamp_limit(args.limit))
    )
    return [_serialize_property(item) for item in session.scalars(stmt)]


def _house_detail(session: Session, args: argparse.Namespace) -> Any:
    item = session.scalar(select(Property).where(Property.id == args.id))
    if item is None:
        return ToolError(error=f"no property found with id={args.id}")
    return _serialize_property(item)


def _auction_search(session: Session, args: argparse.Namespace) -> Any:
    stmt = select(AuctionCase).options(*_EAGER_LOAD)
    if args.city:
        stmt = stmt.where(AuctionCase.city == args.city)
    if args.district:
        stmt = stmt.where(AuctionCase.district == args.district)
    if args.case_type:
        stmt = stmt.where(AuctionCase.case_type == CaseType(args.case_type))
    if args.min_investment_score is not None:
        stmt = stmt.where(AuctionCase.investment_score >= Decimal(str(args.min_investment_score)))
    cases = list(session.scalars(stmt.order_by(desc(AuctionCase.updated_at))))
    # max_floor_price_twd/min_round/deliverable describe the CURRENT round
    # (a child row) or a derived property, not a plain column -- filtered
    # in-process, mirroring how database.repositories.auction.AuctionRepository
    # used to do this (that class is intentionally not present in this
    # container image; see the module docstring).
    if args.max_floor_price_twd is not None:
        cases = [c for c in cases if c.current_round and c.current_round.floor_price_total_twd <= args.max_floor_price_twd]
    if args.min_round is not None:
        cases = [c for c in cases if (c.round_number or 0) >= args.min_round]
    if args.deliverable is not None:
        cases = [c for c in cases if c.is_deliverable is args.deliverable]
    return [_serialize_auction_case(case) for case in cases[: _clamp_limit(args.limit)]]


def _auction_latest(session: Session, args: argparse.Namespace) -> Any:
    stmt = select(AuctionCase).options(*_EAGER_LOAD).order_by(desc(AuctionCase.first_seen_at)).limit(_clamp_limit(args.limit))
    return [_serialize_auction_case(case) for case in session.scalars(stmt)]


def _auction_schedule(session: Session, args: argparse.Namespace) -> Any:
    reference = datetime.now(timezone.utc).date()
    stmt = select(AuctionCase).options(*_EAGER_LOAD).where(AuctionCase.status.in_(ACTIVE_STATUSES))
    cases = list(session.scalars(stmt))
    upcoming: list[AuctionCase] = []
    for case in cases:
        current = case.current_round
        if current is None or current.auction_date is None:
            continue
        days_out = (current.auction_date - reference).days
        if 0 <= days_out <= args.within_days:
            upcoming.append(case)
    upcoming.sort(key=lambda c: c.current_round.auction_date)
    return [_serialize_auction_case(case) for case in upcoming[: _clamp_limit(args.limit)]]


def _auction_detail(session: Session, args: argparse.Namespace) -> Any:
    stmt = (
        select(AuctionCase)
        .options(*_EAGER_LOAD)
        .where(AuctionCase.court_name == args.court_name, AuctionCase.case_number == args.case_number)
    )
    case = session.scalar(stmt)
    if case is None:
        return ToolError(error=f"no case found for court_name={args.court_name!r} case_number={args.case_number!r}")
    result = _serialize_auction_case(case)
    market = session.scalar(
        select(MarketPrice).where(
            MarketPrice.city == case.city,
            MarketPrice.district == case.district,
            MarketPrice.building_type == CASE_TYPE_LABELS.get(case.case_type, "其他"),
        )
    )
    result["regional_average_unit_price_twd"] = market.average_unit_price_twd if market else None
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar_agent_query",
        description="Read-only search/detail queries over Property Case Radar's database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("house-search", help="Search active for-sale listings (一般出售物件)")
    p.add_argument("--city")
    p.add_argument("--district")
    p.add_argument("--max-total-price-twd", type=int, dest="max_total_price_twd")
    p.add_argument("--min-building-area-ping", type=float, dest="min_building_area_ping")
    p.add_argument("--max-age-years", type=float, dest="max_age_years")
    p.add_argument("--min-discount-rate", type=float, dest="min_discount_rate")
    p.add_argument(
        "--property-type",
        choices=(
            "house",
            "land",
            "farmland",
            "building_land",
            "residential_land",
            "commercial_land",
            "industrial_land",
            "forest_land",
            "hillside_land",
            "road_land",
        ),
        dest="property_type",
    )
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(handler=_house_search)

    p = sub.add_parser("house-latest", help="Most recently discovered active listings")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(handler=_house_latest)

    p = sub.add_parser("house-detail", help="One listing by database id")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(handler=_house_detail)

    p = sub.add_parser("auction-search", help="Search auction cases (法拍屋案件)")
    p.add_argument("--city")
    p.add_argument("--district")
    p.add_argument("--case-type", dest="case_type", choices=[c.value for c in CaseType])
    p.add_argument("--max-floor-price-twd", type=int, dest="max_floor_price_twd")
    p.add_argument("--min-round", type=int, dest="min_round")
    p.add_argument("--deliverable", type=lambda v: v.lower() in ("1", "true", "yes"), default=None)
    p.add_argument("--min-investment-score", type=float, dest="min_investment_score")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(handler=_auction_search)

    p = sub.add_parser("auction-latest", help="Most recently discovered auction cases")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(handler=_auction_latest)

    p = sub.add_parser("auction-schedule", help="Active cases auctioning within N days")
    p.add_argument("--within-days", type=int, dest="within_days", default=7)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(handler=_auction_schedule)

    p = sub.add_parser("auction-detail", help="One case by court name + case number")
    p.add_argument("--court-name", dest="court_name", required=True)
    p.add_argument("--case-number", dest="case_number", required=True)
    p.set_defaults(handler=_auction_detail)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Chinese city/district/case-number text must round-trip as real UTF-8
    # regardless of the host's ambient console codepage (e.g. Windows cp950
    # during local dev) -- stdout here is a machine-readable pipe to Codex,
    # never a terminal whose codepage should govern encoding.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = _build_parser()
    args = parser.parse_args(argv)
    started = time.perf_counter()

    try:
        with _read_only_session() as session:
            result = args.handler(session, args)
    except FailClosed as exc:
        _write_audit_log(
            args,
            outcome="fail_closed",
            error=str(exc),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        print(f"radar_agent_query: {exc}", file=sys.stderr)
        return 2

    if isinstance(result, ToolError):
        _write_audit_log(
            args,
            outcome="not_found",
            result=result,
            error=result.error,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        json.dump({"error": result.error}, sys.stdout, ensure_ascii=False)
        print()
        return 1
    _write_audit_log(
        args,
        outcome="ok",
        result=result,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
