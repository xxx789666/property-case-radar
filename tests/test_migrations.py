"""Migration metadata checks that don't require a live PostgreSQL instance.

A full ``alembic upgrade head`` against a real database is exercised
manually (see docs/README "測試" section) since spinning up Postgres in
CI/local test runs is out of scope for this suite; these tests instead
verify (a) that ``Base.metadata`` -- which ``database/migrations/versions/
0001_sale_core.py`` and ``0002_auction_core.py`` both build on via
``create_all()`` -- actually contains every table both migrations are
supposed to produce, and (b) that Alembic's own revision graph resolves
without needing a database connection at all (``alembic history``).
"""

import subprocess
import sys

from sqlalchemy import create_engine, inspect

from database.models import Base

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
    )
    assert result.returncode == 0, result.stderr
    assert "0001" in result.stdout
    assert "0002" in result.stdout
