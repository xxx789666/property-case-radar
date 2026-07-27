"""Coverage for tools/radar_agent_query.py -- the read-only CLI the OpenAB /
Codex ACP Discord LLM bridge uses to query Radar's data (see
openab/sidecar/AGENTS.md). No live Discord, no network, no writes.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database.models.auction import AuctionCase, AuctionRound
from database.models.sale import Property
from tools.radar_agent_query import (
    MAX_LIMIT,
    FailClosed,
    ToolError,
    _build_parser,
    _clamp_limit,
    _create_read_only_engine,
    _original_auction_pdf_files,
    _read_only_session,
    _require_database_url,
    _serialize_auction_case,
    _serialize_property,
    _verify_read_only,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_SOURCE_PATH = REPO_ROOT / "tools" / "radar_agent_query.py"

_FORBIDDEN_WRITE_TOKENS = (
    "session.add(",
    "session.commit(",
    "session.flush(",
    "session.delete(",
    ".upsert_listing(",
    "SubscriptionRepository(",
    "AuctionSubscriptionRepository(",
    ".deactivate(",
    ".create(",
)

# Import names that must never appear in this tool's module-level imports --
# they're either write-capable (repositories) or provide a fallback default
# connection string (apps.config) that this tool must never be able to reach.
_FORBIDDEN_IMPORT_MODULES = (
    "database.repositories",
    "database.repositories.auction",
    "database.repositories.sale",
    "apps.config",
    "apps",
    "crawlers",
    "crawlers.sale",
    "crawlers.sale.base",
    "discord",
)


def _code_only_source() -> str:
    """This file's source with the module docstring stripped -- the
    docstring necessarily names these methods/modules in prose to explain
    the guarantee, so scanning it verbatim would false-positive.
    """
    full_source = TOOL_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(full_source)
    docstring = ast.get_docstring(tree, clean=False) or ""
    return full_source.replace(docstring, "", 1) if docstring else full_source


def test_tool_source_never_references_any_write_operation() -> None:
    """Static, source-level guarantee that this module cannot mutate the
    database, independent of what any individual handler happens to call
    today -- a future edit that adds a write call fails this test even
    before code review catches it.
    """
    code_only = _code_only_source()
    for token in _FORBIDDEN_WRITE_TOKENS:
        assert token not in code_only, f"radar_agent_query.py must never call {token!r}"


def test_tool_never_imports_write_capable_or_fallback_modules() -> None:
    """The write-capable repository classes, the crawlers package they
    depend on, and apps.config's fallback default DATABASE_URL must not
    even be importable from this module -- not merely unused by it. A
    future edit that adds one of these imports (e.g. to "helpfully" reuse
    a repository method) fails here before it can reintroduce a write path
    or a default-credential fallback.
    """
    tree = ast.parse(TOOL_SOURCE_PATH.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden_hit = imported_modules & set(_FORBIDDEN_IMPORT_MODULES)
    assert not forbidden_hit, f"radar_agent_query.py must never import {forbidden_hit}"


def test_clamp_limit_bounds_to_max_and_at_least_one() -> None:
    assert _clamp_limit(5) == 5
    assert _clamp_limit(0) == 1
    assert _clamp_limit(-10) == 1
    assert _clamp_limit(9999) == MAX_LIMIT


@pytest.fixture
def sample_property() -> Property:
    now = datetime.now(timezone.utc)
    return Property(
        source="fixture",
        source_property_id="1",
        url="https://example.invalid/1",
        city="台北市",
        district="大安區",
        total_price_twd=20_000_000,
        unit_price_per_ping_twd=1_000_000,
        building_area_ping=20,
        score=88,
        first_seen_at=now,
        last_seen_at=now,
    )


@pytest.fixture
def sample_case() -> AuctionCase:
    now = datetime.now(timezone.utc)
    return AuctionCase(
        court_name="桃園地方法院",
        case_number="115年度司執字第12345號",
        first_seen_at=now,
        updated_at=now,
        city="桃園市",
        district="中壢區",
        debtor="王小明",
        owner="陳大文",
        occupancy_note="占用人王小明拒絕遷讓",
        rounds=[
            AuctionRound(
                round_number=1, floor_price_total_twd=9_000_000, floor_unit_price_twd=200_000, auction_date=date(2026, 9, 1)
            )
        ],
    )


def test_serialize_property_has_no_debtor_owner_fields(sample_property: Property) -> None:
    # Property (一般出售物件) never carries natural-person fields at all --
    # asserting this keeps the serializer from silently growing one later
    # without a masking decision being made.
    result = _serialize_property(sample_property)
    assert "debtor" not in result
    assert "owner" not in result
    assert result["city"] == "台北市"
    assert result["score"] == 88


def test_serialize_auction_case_masks_debtor_and_owner(sample_case: AuctionCase) -> None:
    result = _serialize_auction_case(sample_case)
    assert result["debtor"] == "王○○"
    assert result["owner"] == "陳○○"
    # occupancy_note is free text that can itself name a person (per
    # notifications/auction_notification.py's public-audience handling) --
    # the agent tool never surfaces it at all.
    assert "occupancy_note" not in result
    assert result["round_number"] == 1
    assert result["floor_price_total_twd"] == 9_000_000


def test_original_pdf_locator_uses_current_capture_hierarchy_and_excludes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = AuctionCase(
        court_name="法務部行政執行署",
        case_number="1050100025324",
        city="桃園市",
        district="中壢區",
    )
    case_dir = tmp_path / "桃園市" / "中壢區" / case.case_number
    case_dir.mkdir(parents=True)
    (case_dir / "1050100025324_1_1.pdf").write_bytes(b"%PDF-original")
    (case_dir / "案件詳細資料.pdf").write_bytes(b"%PDF-summary")
    (case_dir / "1050100025324_notes.pdf").write_bytes(b"%PDF-not-official")
    monkeypatch.setenv("RADAR_AUCTION_DOWNLOAD_DIR", str(tmp_path))

    assert _original_auction_pdf_files(case) == ["1050100025324_1_1.pdf"]
    assert _serialize_auction_case(case)["original_pdf_files"] == ["1050100025324_1_1.pdf"]


def test_original_pdf_locator_supports_legacy_case_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = AuctionCase(
        court_name="法務部行政執行署",
        case_number="1050100025324",
        city="桃園市",
        district="中壢區",
    )
    legacy_dir = tmp_path / case.case_number
    legacy_dir.mkdir()
    (legacy_dir / "1050100025324_2_1.PDF").write_bytes(b"%PDF-original")
    monkeypatch.setenv("RADAR_AUCTION_DOWNLOAD_DIR", str(tmp_path))

    assert _original_auction_pdf_files(case) == ["1050100025324_2_1.PDF"]


def test_original_pdf_locator_returns_empty_without_operator_root(
    sample_case: AuctionCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RADAR_AUCTION_DOWNLOAD_DIR", raising=False)
    assert _original_auction_pdf_files(sample_case) == []


class TestDatabaseUrlFailsClosed:
    """RADAR_AGENT_DATABASE_URL (rendered here as DATABASE_URL by
    openab/sidecar/bridge-server.mjs, which reads the Docker secret file
    directly and passes it only to the codex-acp child it spawns) is
    mandatory and format-checked -- no fallback to apps.config.Settings'
    hardcoded development default anywhere in this path.
    """

    def test_missing_env_var_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(FailClosed, match="DATABASE_URL is not set"):
            _require_database_url()

    def test_blank_env_var_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "   ")
        with pytest.raises(FailClosed, match="DATABASE_URL is not set"):
            _require_database_url()

    @pytest.mark.parametrize(
        "bad_url",
        [
            "mysql://evil:evil@host/db",
            "postgresql://radar:change-me@localhost/radar",  # missing +psycopg
            "not-a-url-at-all",
            "",
        ],
    )
    def test_disallowed_scheme_fails_closed(self, monkeypatch: pytest.MonkeyPatch, bad_url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", bad_url)
        with pytest.raises(FailClosed):
            _require_database_url()

    def test_apps_config_default_is_never_reachable_as_a_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard for the exact default apps/config.py's Settings
        # ships -- confirms this tool treats it as just another string, not
        # as something it would ever silently adopt.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(FailClosed):
            _require_database_url()

    def test_valid_sqlite_url_is_accepted(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        db_path = tmp_path / "ok.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path.as_posix()}")
        assert _require_database_url() == f"sqlite+pysqlite:///{db_path.as_posix()}"


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def real_postgres():
    """A real, disposable, freshly-provisioned PostgreSQL 16 container --
    not a live/shared instance, never real credentials -- with the
    application schema created and the dedicated read-only role from
    openab/sidecar/bootstrap_read_only_role.sql already bootstrapped.
    Module-scoped so the whole file only pays this cost once. Only skips
    if Docker itself isn't installed at all (a real environment-capability
    gap, not a convenience opt-out) -- this is what makes the
    write-capable-DSN-fails / read-only-role-passes tests in this file
    non-skippable in any environment that actually has Docker, per the
    task's requirement.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed in this environment")

    port = _free_tcp_port()
    container_name = f"radar-agent-pg-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-e",
            "POSTGRES_DB=radar",
            "-e",
            "POSTGRES_USER=radar",
            "-e",
            "POSTGRES_PASSWORD=change-me",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        write_dsn = f"postgresql+psycopg://radar:change-me@127.0.0.1:{port}/radar"

        # The official postgres image runs initdb, briefly starts a
        # *temporary* server to apply init scripts, stops it, then starts
        # the real one -- pg_isready can report success against that
        # temporary server, and a connection attempt made in that narrow
        # window gets "server closed the connection unexpectedly" once it
        # stops. Retrying an actual SQL connection (not just pg_isready)
        # until one fully succeeds sidesteps that window entirely.
        import psycopg

        last_error: Exception | None = None
        for _ in range(30):
            try:
                probe = psycopg.connect(
                    f"postgresql://radar:change-me@127.0.0.1:{port}/radar", connect_timeout=3
                )
                probe.close()
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1)
        else:
            raise RuntimeError(f"disposable test Postgres did not become ready in time: {last_error}")

        from database.models import Base
        from database.session import create_db_engine

        engine = create_db_engine(write_dsn)
        Base.metadata.create_all(engine)
        engine.dispose()

        ro_password = "ro-test-password-not-a-real-secret"
        bootstrap_sql = (REPO_ROOT / "openab" / "sidecar" / "bootstrap_read_only_role.sql").read_text(
            encoding="utf-8"
        )
        # Real invocation shape: the password is a psql variable
        # (-v ro_password=...), never a text-substituted literal -- see
        # bootstrap_read_only_role.sql's own header comment and
        # openab/README.md's "bootstrap" section.
        apply_bootstrap = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container_name,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "radar",
                "-d",
                "radar",
                "-v",
                f"ro_password={ro_password}",
            ],
            input=bootstrap_sql,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert apply_bootstrap.returncode == 0, apply_bootstrap.stderr

        ro_dsn = f"postgresql+psycopg://radar_agent_ro:{ro_password}@127.0.0.1:{port}/radar"
        yield {"write_dsn": write_dsn, "ro_dsn": ro_dsn, "port": port, "container_name": container_name}
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=20)


class TestReadOnlyDatabaseBoundary:
    """The actual, technical enforcement: a write attempt must be rejected
    by the database/OS itself, not merely absent from this tool's own code
    paths -- proving a prompt-injected or otherwise adversarial Codex
    session cannot bypass the tool's subcommands and write directly via the
    ORM session or raw SQL.
    """

    @pytest.fixture
    def seeded_db_path(self, tmp_path) -> Path:
        from database.models import Base
        from database.session import create_db_engine, create_session_factory

        db_path = tmp_path / "readonly.db"
        engine = create_db_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory() as session:
            session.add(
                AuctionCase(
                    court_name="桃園地方法院",
                    case_number="115年度司執字第00099號",
                    first_seen_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    city="桃園市",
                    district="中壢區",
                )
            )
            session.commit()
        engine.dispose()
        return db_path

    def test_read_only_session_can_query_a_normally_seeded_database(self, seeded_db_path: Path) -> None:
        url = f"sqlite+pysqlite:///{seeded_db_path.as_posix()}"
        engine = _create_read_only_engine(url)
        try:
            with Session(engine) as session:
                _verify_read_only(session, url)  # must not raise
                case = session.scalar(select(AuctionCase))
                assert case is not None
                assert case.case_number == "115年度司執字第00099號"
        finally:
            engine.dispose()

    def test_direct_orm_insert_is_rejected_by_the_database_not_the_tool(self, seeded_db_path: Path) -> None:
        """Simulates an adversarial session bypassing every subcommand and
        writing straight through the ORM -- must fail at the SQLite engine
        level (a real OS-level read-only file handle), never merely because
        this tool's code chose not to call it.
        """
        url = f"sqlite+pysqlite:///{seeded_db_path.as_posix()}"
        engine = _create_read_only_engine(url)
        try:
            with Session(engine) as session:
                session.add(
                    AuctionCase(
                        court_name="偽造法院",
                        case_number="INJECTED",
                        first_seen_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                with pytest.raises(OperationalError, match="readonly database"):
                    session.commit()
        finally:
            engine.dispose()

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE auction_cases SET city = 'hacked'",
            "DELETE FROM auction_cases",
            "CREATE TABLE evil (id INTEGER)",
            "DROP TABLE auction_cases",
        ],
    )
    def test_raw_sql_writes_and_ddl_are_all_rejected(self, seeded_db_path: Path, statement: str) -> None:
        url = f"sqlite+pysqlite:///{seeded_db_path.as_posix()}"
        engine = _create_read_only_engine(url)
        try:
            with Session(engine) as session:
                with pytest.raises(OperationalError, match="readonly database"):
                    session.execute(text(statement))
                    session.commit()
        finally:
            engine.dispose()

    def test_full_read_only_session_context_manager_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, seeded_db_path: Path
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{seeded_db_path.as_posix()}")
        with _read_only_session() as session:
            case = session.scalar(select(AuctionCase))
            assert case is not None
            with pytest.raises(OperationalError, match="readonly database"):
                session.execute(text("DELETE FROM auction_cases"))
                session.commit()

    def test_postgres_transaction_read_only_is_enforced(self, real_postgres) -> None:
        """Same session-characteristic guarantee against a real,
        automatically-provisioned disposable PostgreSQL server (not
        skipped -- see the ``real_postgres`` fixture) using the
        write-capable ``radar`` role.
        """
        engine = _create_read_only_engine(real_postgres["write_dsn"])
        try:
            with Session(engine) as session:
                value = session.execute(text("SELECT current_setting('transaction_read_only')")).scalar()
                assert str(value).lower() == "on"
                with pytest.raises(Exception, match="read-only transaction"):
                    session.execute(text("CREATE TABLE evil_pg_test (id int)"))
                    session.commit()
        finally:
            engine.dispose()


class TestRealPostgresRolePrivileges:
    """Not skippable in an environment with Docker (verified against a
    real, automatically-provisioned, disposable PostgreSQL server --
    never a live/shared instance, never real credentials): the
    write-capable ``radar`` DSN must fail this tool's own startup
    verification outright, the dedicated ``radar_agent_ro`` role (per
    ``openab/sidecar/bootstrap_read_only_role.sql``) must pass it and
    serve real queries, and a write attempt through the read-only role --
    including one that first tries to override the session characteristic
    back to READ WRITE -- must still be rejected by PostgreSQL's actual
    privilege system, not merely by this tool's own session-level choice.
    This includes TEMP tables and sequence nextval(), both of which
    PostgreSQL grants some form of by default and which a naive
    "SELECT-only" role could otherwise still exploit as a write path.
    """

    def test_write_capable_dsn_fails_startup_verification(self, real_postgres, monkeypatch: pytest.MonkeyPatch) -> None:
        # Docker's official postgres image always creates POSTGRES_USER
        # (here "radar", the main app's role) as an actual PostgreSQL
        # superuser -- so this is caught by the role-attribute check
        # specifically (rolsuper), not the per-table privilege scan. Both
        # are checks inside the same _verify_role_has_no_write_privileges
        # fail-closed function; a role that's merely "not superuser but
        # still has table GRANTs" would instead be caught by the
        # write-privilege scan (exercised indirectly by the dedicated
        # read-only role's grants being deliberately minimal below).
        monkeypatch.setenv("DATABASE_URL", real_postgres["write_dsn"])
        with pytest.raises(FailClosed, match="dangerous role attribute"):
            with _read_only_session():
                pass

    def test_dedicated_read_only_role_passes_and_serves_queries(
        self, real_postgres, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", real_postgres["ro_dsn"])
        with _read_only_session() as session:
            # Verification itself didn't raise (fixture asserts this
            # implicitly); confirm the role can still actually read.
            count = session.execute(text("SELECT count(*) FROM auction_cases")).scalar()
            assert count is not None

    def test_read_only_role_direct_bypass_write_attempt_is_rejected(self, real_postgres) -> None:
        """Connects with the read-only role's DSN directly via a fresh
        engine that never runs this tool's own
        ``_force_read_only_transaction`` connect hook at all -- simulating
        adversarial/prompt-injected code that bypasses
        tools/radar_agent_query.py entirely and talks to Postgres on its
        own. The rejection here comes purely from the role's GRANTs.
        """
        import psycopg

        with psycopg.connect(real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")) as conn:
            conn.autocommit = True
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE auction_cases SET city = 'hacked'")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE evil_bypass_test (id int)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO auction_cases (court_name, case_number) VALUES ('x', 'y')")

    def test_read_only_role_rejected_even_after_explicit_read_write_override(self, real_postgres) -> None:
        """Proves the boundary is the role's own GRANTs, not this tool's
        session-characteristic trick: explicitly asks Postgres for a
        READ WRITE transaction (the opposite of what this tool would ever
        request) using the dedicated read-only role, and confirms the
        actual write attempt is still rejected on privilege grounds.
        """
        import psycopg

        with psycopg.connect(real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")) as conn:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SET TRANSACTION READ WRITE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE auction_cases SET city = 'hacked'")
            conn.rollback()

    def test_read_only_role_cannot_create_temp_table_even_after_read_write_override(self, real_postgres) -> None:
        """TEMP is granted to PUBLIC on every PostgreSQL database by
        default -- bootstrap_read_only_role.sql explicitly revokes it from
        both PUBLIC and radar_agent_ro. Without that REVOKE, this exact
        statement would succeed even for a role with no other privilege at
        all, since CREATE TEMPORARY TABLE only needs database-level TEMP,
        not any table-level GRANT. Also proves database state is
        unaffected: the transaction is rolled back and a fresh connection
        confirms the row count is unchanged.
        """
        import psycopg

        before_dsn = real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(before_dsn) as probe:
            before_count = probe.execute("SELECT count(*) FROM auction_cases").fetchone()[0]

        with psycopg.connect(before_dsn) as conn:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SET TRANSACTION READ WRITE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("CREATE TEMP TABLE evil_temp_test (id int)")
            conn.rollback()

        with psycopg.connect(before_dsn) as probe:
            after_count = probe.execute("SELECT count(*) FROM auction_cases").fetchone()[0]
        assert after_count == before_count

    def test_read_only_role_cannot_call_nextval_even_after_read_write_override(self, real_postgres) -> None:
        """Sequence USAGE alone (independent of UPDATE) is enough to call
        nextval()/currval() and permanently advance a sequence's counter --
        a real write capability a "SELECT only" role must not retain.
        bootstrap_read_only_role.sql revokes ALL (not just UPDATE) on every
        sequence. Uses whichever real sequence backs an existing table's
        primary key, since that is what this role's grants must actually
        cover, not a sequence created for this test.
        """
        import psycopg

        dsn = real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as probe:
            sequence_name = probe.execute(
                "SELECT pg_get_serial_sequence('auction_cases', 'id')"
            ).fetchone()[0]
        assert sequence_name, "auction_cases.id must be backed by a real sequence for this test to be meaningful"

        with psycopg.connect(dsn) as conn:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SET TRANSACTION READ WRITE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"SELECT nextval('{sequence_name}')")
            conn.rollback()

    def test_read_only_role_has_no_temp_or_create_database_privilege(self, real_postgres) -> None:
        """Direct catalog check (has_database_privilege), independent of
        any specific statement attempt above -- pins the exact privilege
        this tool's own _verify_role_has_no_write_privileges checks for.
        """
        import psycopg

        dsn = real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as conn:
            can_temp = conn.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'TEMP')"
            ).fetchone()[0]
            can_create = conn.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            ).fetchone()[0]
        assert can_temp is False
        assert can_create is False

    def test_read_only_role_has_no_sequence_usage_or_update_privilege(self, real_postgres) -> None:
        import psycopg

        dsn = real_postgres["ro_dsn"].replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn) as conn:
            sequence_name = conn.execute("SELECT pg_get_serial_sequence('auction_cases', 'id')").fetchone()[0]
            can_usage = conn.execute(
                "SELECT has_sequence_privilege(current_user, %s, 'USAGE')", (sequence_name,)
            ).fetchone()[0]
            can_update = conn.execute(
                "SELECT has_sequence_privilege(current_user, %s, 'UPDATE')", (sequence_name,)
            ).fetchone()[0]
        assert can_usage is False
        assert can_update is False

    def test_bootstrap_sql_fails_closed_without_ro_password(self, real_postgres) -> None:
        """Running the real bootstrap script without -v ro_password=... must
        exit non-zero -- psql's own \\quit meta-command has no exit-code
        argument (verified against a real psql 16: it always exits 0),
        which would let automation mistake this abort for a successful
        bootstrap. Covers both the caller passing -v ON_ERROR_STOP=1
        explicitly and not, since the script's own \\set ON_ERROR_STOP on
        must make this fail-closed either way.
        """
        bootstrap_sql = (REPO_ROOT / "openab" / "sidecar" / "bootstrap_read_only_role.sql").read_text(
            encoding="utf-8"
        )
        for psql_args in (
            ["psql", "-U", "radar", "-d", "radar"],
            ["psql", "-v", "ON_ERROR_STOP=1", "-U", "radar", "-d", "radar"],
        ):
            result = subprocess.run(
                ["docker", "exec", "-i", real_postgres["container_name"], *psql_args],
                input=bootstrap_sql,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode != 0, (
                f"bootstrap must fail closed (nonzero exit) without ro_password; args={psql_args}"
            )
            assert "ro_password variable is not set" in result.stderr


class TestHandlersAgainstARealSession:
    """Exercise each subcommand's handler through real argparse parsing and
    a real (SQLite, in-memory) SQLAlchemy session -- the same session_factory
    fixture the rest of this repo's tests use.
    """

    def test_house_search_and_latest_and_detail(self, session_factory, sample_property: Property) -> None:
        with session_factory() as session:
            session.add(sample_property)
            session.commit()
            property_id = sample_property.id

            parser = _build_parser()
            args = parser.parse_args(["house-search", "--city", "台北市", "--limit", "5"])
            results = args.handler(session, args)
            assert len(results) == 1
            assert results[0]["id"] == property_id

            args = parser.parse_args(["house-latest", "--limit", "5"])
            results = args.handler(session, args)
            assert len(results) == 1

            args = parser.parse_args(["house-detail", "--id", str(property_id)])
            result = args.handler(session, args)
            assert result["id"] == property_id

            args = parser.parse_args(["house-detail", "--id", "999999"])
            result = args.handler(session, args)
            assert isinstance(result, ToolError)

    def test_house_search_filters_land_subtypes(self, session_factory) -> None:
        with session_factory() as session:
            common = {
                "source": "591",
                "url": "https://land.591.com.tw/sale/example",
                "city": "桃園市",
                "district": "中壢區",
                "address": "測試段",
                "total_price_twd": 20_000_000,
                "unit_price_per_ping_twd": 100_000,
                "building_area_ping": Decimal("200"),
                "land_area_ping": Decimal("200"),
                "building_type": "土地",
                "status": "active",
            }
            session.add_all(
                [
                    Property(
                        **common,
                        source_property_id="land-farm",
                        usage="農地",
                    ),
                    Property(
                        **common,
                        source_property_id="land-build",
                        usage="住宅用地/建地",
                    ),
                    Property(
                        source="591",
                        source_property_id="house",
                        url="https://sale.591.com.tw/house",
                        city="桃園市",
                        district="中壢區",
                        total_price_twd=10_000_000,
                        unit_price_per_ping_twd=300_000,
                        building_area_ping=Decimal("30"),
                        building_type="電梯大樓",
                        status="active",
                    ),
                ]
            )
            session.commit()
            parser = _build_parser()

            land_args = parser.parse_args(
                ["house-search", "--property-type", "land", "--limit", "50"]
            )
            land = land_args.handler(session, land_args)
            assert {row["source_property_id"] for row in land} == {
                "land-farm",
                "land-build",
            }

            farm_args = parser.parse_args(
                ["house-search", "--property-type", "farmland", "--limit", "50"]
            )
            farmland = farm_args.handler(session, farm_args)
            assert [row["usage"] for row in farmland] == ["農地"]

            build_args = parser.parse_args(
                [
                    "house-search",
                    "--property-type",
                    "building_land",
                    "--limit",
                    "50",
                ]
            )
            building_land = build_args.handler(session, build_args)
            assert [row["usage"] for row in building_land] == ["住宅用地/建地"]

    def test_auction_search_latest_schedule_detail(self, session_factory, sample_case: AuctionCase) -> None:
        with session_factory() as session:
            session.add(sample_case)
            session.commit()

            parser = _build_parser()
            args = parser.parse_args(["auction-search", "--city", "桃園市", "--limit", "5"])
            results = args.handler(session, args)
            assert len(results) == 1
            assert results[0]["debtor"] == "王○○"

            args = parser.parse_args(["auction-latest", "--limit", "5"])
            results = args.handler(session, args)
            assert len(results) == 1

            args = parser.parse_args(["auction-schedule", "--within-days", "3650"])
            results = args.handler(session, args)
            assert len(results) == 1

            args = parser.parse_args(
                ["auction-detail", "--court-name", "桃園地方法院", "--case-number", "115年度司執字第12345號"]
            )
            result = args.handler(session, args)
            assert result["case_number"] == "115年度司執字第12345號"
            assert result["debtor"] == "王○○"
            assert result["regional_average_unit_price_twd"] is None  # no MarketPrice seeded

            args = parser.parse_args(["auction-detail", "--court-name", "無此法院", "--case-number", "no-such-case"])
            result = args.handler(session, args)
            assert isinstance(result, ToolError)


def test_cli_end_to_end_via_subprocess_reads_utf8_and_stays_read_only(tmp_path) -> None:
    """The exact invocation shape Codex uses in production: a subprocess,
    DATABASE_URL from the environment, JSON on stdout. Uses a real temp-file
    SQLite database (not :memory:, which isn't visible across processes) and
    never a live Postgres connection.
    """
    db_path = tmp_path / "radar_agent_smoke.db"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"

    seed_script = f"""
import os
os.environ["DATABASE_URL"] = {database_url!r}
from database.models import Base
from database.session import create_db_engine, create_session_factory
from database.models.auction import AuctionCase, AuctionRound
from datetime import date, datetime, timezone

engine = create_db_engine({database_url!r})
Base.metadata.create_all(engine)
factory = create_session_factory(engine)
with factory() as session:
    session.add(AuctionCase(
        court_name="桃園地方法院", case_number="115年度司執字第99999號",
        first_seen_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        city="桃園市", district="中壢區", debtor="王小明", owner="王小明",
        rounds=[AuctionRound(round_number=1, floor_price_total_twd=9000000, floor_unit_price_twd=200000, auction_date=date(2026, 9, 1))],
    ))
    session.commit()
"""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    seed = subprocess.run([sys.executable, "-c", seed_script], capture_output=True, text=True, timeout=30, env=env)
    assert seed.returncode == 0, seed.stderr

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.radar_agent_query",
            "auction-detail",
            "--court-name",
            "桃園地方法院",
            "--case-number",
            "115年度司執字第99999號",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
        env=env,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["case_number"] == "115年度司執字第99999號"
    assert payload["debtor"] == "王○○"  # masked even though this is a "trusted" CLI call

    # The seeded row must still be there and unmodified -- a read-only tool
    # invocation must never alter the database it queries.
    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.radar_agent_query",
            "auction-latest",
            "--limit",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
        env=env,
        encoding="utf-8",
    )
    assert verify.returncode == 0, verify.stderr
    latest = json.loads(verify.stdout)
    assert len(latest) == 1
    assert latest[0]["case_number"] == "115年度司執字第99999號"


def test_cli_rejects_unknown_subcommand() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["drop-database"])
