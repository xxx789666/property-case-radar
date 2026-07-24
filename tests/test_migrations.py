"""Migration determinism/fidelity checks against real SQLite databases.

``database/migrations/versions/0001_sale_core.py`` and
``0002_auction_core.py`` are hand-written ``op.create_table``/
``op.add_column`` DDL -- they do NOT call
``database.models.Base.metadata.create_all()`` (a live-metadata-derived
migration would silently change historical behavior if a model gains or
loses a column later). These tests run the actual Alembic CLI
(``python -m alembic upgrade/downgrade``) against disposable SQLite
files to verify: 0001 alone produces exactly the sale/shared tables (no
auction_* tables yet), 0002 adds exactly the auction tables plus
``notification_logs.auction_case_id``, a downgrade removes exactly what
its own revision added, a full downgrade-then-upgrade round trip is
stable, and the resulting schema is column-for-column identical to what
``database.models``' live SQLAlchemy models would produce (the fidelity
check that keeps the frozen DDL from drifting out of sync with the ORM
it's meant to describe). PostgreSQL-specific behavior (e.g. native
sequence/identity quirks) is out of scope here by design -- SQLite is
sufficient to verify the DDL is well-formed and matches the ORM shape;
a full ``alembic upgrade head`` against real PostgreSQL is exercised
manually per README's "測試" section.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from database.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

_EXPECTED_SALE_TABLES = {
    "properties",
    "property_price_history",
    "property_subscriptions",
    "market_prices",
    "actual_transactions",
    "users",
    "discord_channels",
    "notification_logs",
}

_EXPECTED_AUCTION_TABLES = {
    "auction_cases",
    "auction_rounds",
    "auction_documents",
    "auction_status_history",
    "auction_subscriptions",
}


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env=env,
    )


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_base_metadata_contains_all_sale_and_auction_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert _EXPECTED_SALE_TABLES <= tables
    assert _EXPECTED_AUCTION_TABLES <= tables
    engine.dispose()


def test_notification_logs_has_both_sale_and_auction_link_columns() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("notification_logs")}
    assert {"property_id", "auction_case_id"} <= columns
    engine.dispose()


def test_alembic_history_resolves_without_a_database() -> None:
    # `alembic history` only reads the local script directory -- unlike
    # `upgrade`/`downgrade`, it never opens a database connection, so this
    # is a safe, offline check that the 0001 -> 0002 revision chain is
    # well-formed (no missing down_revision, no branch conflicts).
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "history"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "0001" in result.stdout
    assert "0002" in result.stdout


def test_upgrade_0001_creates_only_sale_and_shared_tables(tmp_path) -> None:
    url = _sqlite_url(tmp_path / "only_0001.db")
    result = _run_alembic("upgrade", "0001", database_url=url)
    assert result.returncode == 0, result.stderr
    assert _table_names(url) == _EXPECTED_SALE_TABLES

    engine = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("notification_logs")}
    finally:
        engine.dispose()
    assert "auction_case_id" not in columns  # not added until 0002


def test_upgrade_head_adds_auction_tables_on_top_of_0001(tmp_path) -> None:
    url = _sqlite_url(tmp_path / "head.db")
    result = _run_alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, result.stderr
    assert _table_names(url) == _EXPECTED_SALE_TABLES | _EXPECTED_AUCTION_TABLES

    engine = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("notification_logs")}
    finally:
        engine.dispose()
    assert {"property_id", "auction_case_id"} <= columns


def test_downgrade_to_0001_removes_only_auction_tables(tmp_path) -> None:
    url = _sqlite_url(tmp_path / "downgrade.db")
    assert _run_alembic("upgrade", "head", database_url=url).returncode == 0
    result = _run_alembic("downgrade", "0001", database_url=url)
    assert result.returncode == 0, result.stderr
    assert _table_names(url) == _EXPECTED_SALE_TABLES

    engine = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("notification_logs")}
    finally:
        engine.dispose()
    assert "auction_case_id" not in columns


def test_full_downgrade_then_upgrade_round_trip_is_stable(tmp_path) -> None:
    url = _sqlite_url(tmp_path / "roundtrip.db")
    assert _run_alembic("upgrade", "head", database_url=url).returncode == 0
    assert _run_alembic("downgrade", "base", database_url=url).returncode == 0
    assert _table_names(url) == set()

    result = _run_alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, result.stderr
    assert _table_names(url) == _EXPECTED_SALE_TABLES | _EXPECTED_AUCTION_TABLES


def test_migrated_schema_matches_live_orm_models_exactly(tmp_path) -> None:
    """The hand-written DDL in 0001/0002 must describe exactly the same
    tables/columns/nullability as database.models' live SQLAlchemy
    definitions -- this is what keeps the frozen migrations from
    silently drifting out of sync with the ORM they're meant to
    describe.
    """
    url = _sqlite_url(tmp_path / "fidelity.db")
    assert _run_alembic("upgrade", "head", database_url=url).returncode == 0

    migrated_engine = create_engine(url)
    live_engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(live_engine)
        migrated_insp = inspect(migrated_engine)
        live_insp = inspect(live_engine)

        migrated_tables = set(migrated_insp.get_table_names()) - {"alembic_version"}
        live_tables = set(live_insp.get_table_names())
        assert migrated_tables == live_tables

        for table in sorted(migrated_tables):
            migrated_cols = {c["name"]: c["nullable"] for c in migrated_insp.get_columns(table)}
            live_cols = {c["name"]: c["nullable"] for c in live_insp.get_columns(table)}
            assert migrated_cols == live_cols, f"{table} column set/nullability mismatch"
    finally:
        migrated_engine.dispose()
        live_engine.dispose()
