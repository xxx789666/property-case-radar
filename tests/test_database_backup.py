import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.backup_postgres import remap_public_schema, remove_expired_backups


def test_remap_public_schema_preserves_copy_payload(tmp_path: Path):
    source = tmp_path / "source.sql"
    destination = tmp_path / "destination.sql"
    source.write_text(
        "CREATE SCHEMA public;\n"
        "CREATE TABLE public.items (value text);\n"
        "COPY public.items (value) FROM stdin;\n"
        "public.must_remain_data\n"
        "\\.\n"
        "SELECT pg_catalog.setval('public.items_id_seq', 1, true);\n",
        encoding="utf-8",
    )

    remap_public_schema(source, destination, "backup_verify_abc")

    result = destination.read_text(encoding="utf-8")
    assert "CREATE SCHEMA backup_verify_abc;" in result
    assert "CREATE TABLE backup_verify_abc.items" in result
    assert "COPY backup_verify_abc.items" in result
    assert "public.must_remain_data" in result
    assert "'backup_verify_abc.items_id_seq'" in result


def test_remove_expired_backups_only_removes_matching_old_files(tmp_path: Path):
    old_dump = tmp_path / "radar_20200101_000000.dump"
    old_hash = tmp_path / "radar_20200101_000000.dump.sha256"
    current_dump = tmp_path / "radar_20990101_000000.dump"
    unrelated = tmp_path / "notes.txt"
    for path in (old_dump, old_hash, current_dump, unrelated):
        path.write_text("x", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(old_dump, (old_time, old_time))
    os.utime(old_hash, (old_time, old_time))

    removed = remove_expired_backups(tmp_path, retention_days=14)

    assert removed == 2
    assert not old_dump.exists()
    assert not old_hash.exists()
    assert current_dump.exists()
    assert unrelated.exists()
