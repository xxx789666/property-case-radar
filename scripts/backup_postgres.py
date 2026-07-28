from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.config import get_settings


POSTGRES_BIN_DIRS = (
    Path(r"C:\Program Files\PostgreSQL\17\bin"),
    Path(r"C:\Program Files\PostgreSQL\16\bin"),
    Path(r"C:\Program Files\PostgreSQL\15\bin"),
)


def find_postgres_tool(name: str) -> Path:
    executable = f"{name}.exe" if os.name == "nt" else name
    from_path = shutil.which(executable)
    if from_path:
        return Path(from_path)
    for directory in POSTGRES_BIN_DIRS:
        candidate = directory / executable
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"PostgreSQL tool not found: {executable}")


def subprocess_environment(url: URL) -> dict[str, str]:
    environment = os.environ.copy()
    if url.password is not None:
        environment["PGPASSWORD"] = url.password
    return environment


def connection_arguments(url: URL) -> list[str]:
    arguments = ["--host", url.host or "127.0.0.1"]
    if url.port is not None:
        arguments.extend(["--port", str(url.port)])
    if url.username is not None:
        arguments.extend(["--username", url.username])
    arguments.extend(["--dbname", url.database or "postgres"])
    return arguments


def remap_public_schema(source: Path, destination: Path, schema: str) -> None:
    """Remap pg_restore SQL while leaving raw COPY payloads untouched."""
    in_copy_data = False
    with source.open("r", encoding="utf-8") as reader, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as writer:
        writer.write(f"CREATE SCHEMA {schema};\n")
        for line in reader:
            if in_copy_data:
                writer.write(line)
                if line.rstrip("\r\n") == r"\.":
                    in_copy_data = False
                continue

            if re.match(r"^COPY\s+public\.", line):
                in_copy_data = True

            if re.match(r"^\s*CREATE\s+SCHEMA\s+public\s*;", line):
                continue
            line = re.sub(r"\bpublic\.", f"{schema}.", line)
            line = re.sub(r"(\bSCHEMA\s+)public\b", rf"\g<1>{schema}", line)
            line = re.sub(
                r"(\bsearch_path\s*=\s*)public\b",
                rf"\g<1>{schema}",
                line,
                flags=re.IGNORECASE,
            )
            writer.write(line)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_expired_backups(directory: Path, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    removed = 0
    for pattern in ("radar_*.dump", "radar_*.dump.sha256"):
        for path in directory.glob(pattern):
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified < cutoff:
                path.unlink()
                removed += 1
    return removed


def verify_restore(
    archive: Path,
    *,
    url: URL,
    pg_restore: Path,
    psql: Path,
) -> tuple[int, int]:
    schema = f"backup_verify_{uuid4().hex}"
    environment = subprocess_environment(url)
    engine = create_engine(url)
    try:
        with tempfile.TemporaryDirectory(prefix="radar-backup-verify-") as temp:
            plain_sql = Path(temp) / "restore.sql"
            remapped_sql = Path(temp) / "restore-remapped.sql"
            with plain_sql.open("wb") as output:
                subprocess.run(
                    [
                        str(pg_restore),
                        "--no-owner",
                        "--no-privileges",
                        "--file",
                        "-",
                        str(archive),
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                    env=environment,
                )
            remap_public_schema(plain_sql, remapped_sql, schema)
            try:
                subprocess.run(
                    [
                        str(psql),
                        *connection_arguments(url),
                        "--set",
                        "ON_ERROR_STOP=1",
                        "--single-transaction",
                        "--file",
                        str(remapped_sql),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=True,
                    env=environment,
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"restore verification failed: {detail}") from error

        with engine.connect() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                ),
                {"schema": schema},
            ).scalars().all()
            if not tables:
                raise RuntimeError("restore verification produced no tables")
            populated = 0
            for table_name in tables:
                safe_table = table_name.replace('"', '""')
                count = connection.execute(
                    text(f'SELECT count(*) FROM "{schema}"."{safe_table}"')
                ).scalar_one()
                if count:
                    populated += 1
            if not populated:
                raise RuntimeError("restore verification produced no readable rows")
            return len(tables), populated
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            engine.dispose()


def create_verified_backup() -> Path:
    settings = get_settings()
    url = make_url(settings.database_url)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("database backup requires PostgreSQL")

    directory = settings.database_backup_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    final_archive = directory / f"radar_{timestamp}.dump"
    partial_archive = final_archive.with_suffix(".dump.partial")

    pg_dump = find_postgres_tool("pg_dump")
    pg_restore = find_postgres_tool("pg_restore")
    psql = find_postgres_tool("psql")
    environment = subprocess_environment(url)

    try:
        subprocess.run(
            [
                str(pg_dump),
                *connection_arguments(url),
                "--format",
                "custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(partial_archive),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            env=environment,
        )
        tables, populated = verify_restore(
            partial_archive,
            url=url,
            pg_restore=pg_restore,
            psql=psql,
        )
        partial_archive.replace(final_archive)
        checksum = sha256_file(final_archive)
        final_archive.with_suffix(".dump.sha256").write_text(
            f"{checksum}  {final_archive.name}\n",
            encoding="ascii",
        )
        removed = remove_expired_backups(
            directory, settings.database_backup_retention_days
        )
        print(
            f"backup verified: {final_archive} "
            f"(tables={tables}, populated={populated}, expired_removed={removed})"
        )
        return final_archive
    except Exception:
        partial_archive.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    from apps.services.system_alerts import update_system_alert

    active_settings = get_settings()
    try:
        archive_path = create_verified_backup()
    except Exception as backup_error:
        update_system_alert(
            active_settings,
            key="database-backup-job",
            failing=True,
            title="PostgreSQL 備份工作",
            detail=str(backup_error)[:1500],
        )
        raise
    else:
        update_system_alert(
            active_settings,
            key="database-backup-job",
            failing=False,
            title="PostgreSQL 備份工作",
            detail=f"已完成並驗證：{archive_path.name}",
        )
