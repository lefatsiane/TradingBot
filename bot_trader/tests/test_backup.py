from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from tools.backup import backup_database


def test_backup_uses_sqlite_backup_api_and_prunes_after_14_days(tmp_path) -> None:
    source = tmp_path / "state.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('durable')")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_backup = backup_dir / "bot-state-20261011.sqlite3"
    old_backup.write_bytes(b"old")
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)

    result = backup_database(source, backup_dir, now_utc=now)

    assert result.name == "bot-state-20261026.sqlite3"
    with sqlite3.connect(result) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "durable",
        )
    assert not old_backup.exists()


def test_backup_retains_files_inside_the_14_day_window(tmp_path) -> None:
    source = tmp_path / "state.sqlite3"
    sqlite3.connect(source).close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    recent_backup = backup_dir / "bot-state-20261013.sqlite3"
    recent_backup.write_bytes(b"recent")

    backup_database(
        source,
        backup_dir,
        now_utc=datetime(2026, 10, 26, tzinfo=UTC),
    )

    assert recent_backup.exists()


def test_backup_rejects_missing_database(tmp_path) -> None:
    try:
        backup_database(
            tmp_path / "missing.sqlite3",
            tmp_path / "backups",
            now_utc=datetime.now(UTC) + timedelta(days=1),
        )
    except FileNotFoundError:
        return
    raise AssertionError("A missing source database must not report a backup")
