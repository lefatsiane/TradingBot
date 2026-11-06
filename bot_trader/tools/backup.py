from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BACKUP_NAME = re.compile(r"^bot-state-(\d{8})\.sqlite3$")
_RETENTION = timedelta(days=14)


def backup_database(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    now_utc: datetime | None = None,
) -> Path:
    source_path = Path(database_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"State database does not exist: {source_path}")
    current_time = now_utc or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current_time = current_time.astimezone(UTC)

    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target_path = directory / f"bot-state-{current_time:%Y%m%d}.sqlite3"
    temporary_path = target_path.with_suffix(".sqlite3.tmp")
    if source_path.resolve() in (target_path.resolve(), temporary_path.resolve()):
        raise ValueError("The state database cannot be stored in its backup path")

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(temporary_path)
        source.backup(destination)
        destination.close()
        destination = None
        os.replace(temporary_path, target_path)
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        temporary_path.unlink(missing_ok=True)

    _prune_old_backups(directory, current_time)
    return target_path


def _prune_old_backups(directory: Path, now_utc: datetime) -> None:
    cutoff_date = (now_utc - _RETENTION).date()
    for candidate in directory.iterdir():
        match = _BACKUP_NAME.fullmatch(candidate.name)
        if match is None or not candidate.is_file():
            continue
        backup_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        if backup_date < cutoff_date:
            candidate.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified SQLite state backup.")
    parser.add_argument("--database", default="state/bot.sqlite3")
    parser.add_argument("--backup-dir", default="backups")
    args = parser.parse_args()
    try:
        destination = backup_database(args.database, args.backup_dir)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Backup failed: {error}", file=sys.stderr)
        return 2
    print(f"State database backed up to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
