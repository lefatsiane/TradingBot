"""Safely import manually entered economic calendar events."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
APPROVED_CALENDAR = CONFIG_DIR / "news.csv"
CANDIDATE_CALENDAR = CONFIG_DIR / "news.candidate.csv"
TEMPLATE_CALENDAR = CONFIG_DIR / "news_input.template.csv"
BACKUP_DIR = PROJECT_ROOT / "backups" / "news-calendar"
INPUT_COLUMNS = ("date", "time", "currency", "impact", "title")
OUTPUT_COLUMNS = ("time_utc", "currency", "impact", "title")
ALLOWED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD"})
ALLOWED_IMPACTS = frozenset({"high", "medium", "low"})
DEFAULT_IMPACTS = frozenset({"high", "medium"})

from bot.app.config import load_settings
from bot.app.news import NewsEvent, load_news_calendar, validate_news_calendar


class InputValidationError(ValueError):
    def __init__(self, problems: list[str], warnings: list[str]) -> None:
        self.problems = problems
        self.warnings = warnings
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class CheckedInput:
    events: tuple[NewsEvent, ...]
    warnings: tuple[str, ...]


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_key(event: NewsEvent) -> tuple[datetime, str, str]:
    return event.time_utc, event.currency, event.title


def _local_to_utc(value: datetime, zone: ZoneInfo, *, row_number: int) -> datetime:
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(f"row {row_number}: local time is ambiguous in the selected timezone")

    converted = first.astimezone(UTC)
    round_trip = converted.astimezone(zone).replace(tzinfo=None)
    if round_trip != value:
        raise ValueError(f"row {row_number}: local time does not exist in the selected timezone")
    return converted


def check_input(
    input_path: str | Path,
    *,
    input_timezone: str = "UTC",
    now_utc: datetime | None = None,
) -> CheckedInput:
    try:
        zone = ZoneInfo(input_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown input timezone: {input_timezone}") from error

    current_time = now_utc or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current_time = current_time.astimezone(UTC)

    problems: list[str] = []
    warnings: list[str] = []
    events: list[NewsEvent] = []
    seen: set[tuple[datetime, str, str]] = set()

    try:
        with Path(input_path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("Input file is missing a header row (row 1)")
            missing_columns = sorted(set(INPUT_COLUMNS) - set(reader.fieldnames))
            extra_columns = sorted(set(reader.fieldnames) - set(INPUT_COLUMNS))
            if missing_columns:
                problems.append(f"row 1: missing column(s): {', '.join(missing_columns)}")
            if extra_columns:
                problems.append(f"row 1: unexpected column(s): {', '.join(extra_columns)}")
            if len(set(reader.fieldnames)) != len(reader.fieldnames):
                problems.append("row 1: duplicate column name")

            for row_number, row in enumerate(reader, start=2):
                row_problems: list[str] = []
                if None in row:
                    row_problems.append("too many fields for the header")

                values = {column: (row.get(column) or "").strip() for column in INPUT_COLUMNS}
                for column, value in values.items():
                    if not value:
                        row_problems.append(f"{column} is blank")

                if values["time"].casefold() in {"all day", "tentative"}:
                    row_problems.append(f"time is {values['time']!r}; leave this row out")

                local_datetime: datetime | None = None
                if values["date"] and values["time"]:
                    try:
                        parsed_date = datetime.strptime(values["date"], "%Y-%m-%d").date()
                        if parsed_date.isoformat() != values["date"]:
                            raise ValueError
                    except ValueError:
                        row_problems.append("date must use YYYY-MM-DD")
                    else:
                        try:
                            parsed_time = datetime.strptime(values["time"], "%H:%M").time()
                            if parsed_time.strftime("%H:%M") != values["time"]:
                                raise ValueError
                        except ValueError:
                            if values["time"].casefold() not in {"all day", "tentative"}:
                                row_problems.append("time must use HH:MM in 24-hour format")
                        else:
                            local_datetime = datetime.combine(parsed_date, parsed_time)

                currency = values["currency"].upper()
                if values["currency"] and (
                    len(currency) != 3 or currency not in ALLOWED_CURRENCIES
                ):
                    row_problems.append("currency must be one of USD, EUR, GBP, JPY, CHF, AUD, NZD")

                impact = values["impact"].lower()
                if values["impact"] and impact not in ALLOWED_IMPACTS:
                    row_problems.append("impact must be high, medium, or low")

                event_time: datetime | None = None
                if local_datetime is not None:
                    try:
                        event_time = _local_to_utc(
                            local_datetime,
                            zone,
                            row_number=row_number,
                        )
                    except ValueError as error:
                        row_problems.append(str(error))

                if event_time is not None:
                    if event_time < current_time - timedelta(days=1):
                        row_problems.append("event date is more than 1 day in the past")
                    if event_time.weekday() >= 5:
                        row_problems.append("event falls on a Saturday or Sunday in UTC")
                    if event_time > current_time + timedelta(days=21):
                        warnings.append(f"row {row_number}: event is more than 21 days ahead")

                    if currency in ALLOWED_CURRENCIES and impact in ALLOWED_IMPACTS:
                        event = NewsEvent(
                            time_utc=event_time,
                            currency=currency,
                            impact=impact,
                            title=values["title"],
                        )
                        key = _event_key(event)
                        if key in seen:
                            row_problems.append(
                                "duplicate event (same UTC time, currency, and title)"
                            )
                        else:
                            seen.add(key)
                            events.append(event)

                problems.extend(f"row {row_number}: {message}" for message in row_problems)

    except UnicodeDecodeError as error:
        raise ValueError("Input file is not valid UTF-8") from error
    except csv.Error as error:
        raise ValueError(f"Invalid CSV near row {reader.line_num}: {error}") from error

    if problems:
        raise InputValidationError(problems, warnings)
    events.sort(key=lambda event: (event.time_utc, event.currency, event.title))
    return CheckedInput(tuple(events), tuple(warnings))


def _write_events(path: Path, events: list[NewsEvent] | tuple[NewsEvent, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            writer = csv.writer(handle)
            writer.writerow(OUTPUT_COLUMNS)
            for event in events:
                writer.writerow(
                    (_format_utc(event.time_utc), event.currency, event.impact, event.title)
                )
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise


def write_template(path: Path = TEMPLATE_CALENDAR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(INPUT_COLUMNS)
        writer.writerow(
            (
                "EXAMPLE ONLY",
                "08:30",
                "USD",
                "high",
                "[EXAMPLE ONLY] Replace this row with a real event",
            )
        )
        writer.writerow(
            (
                "EXAMPLE ONLY",
                "10:00",
                "EUR",
                "medium",
                "[EXAMPLE ONLY] Delete this row if unused",
            )
        )


def _print_warnings(warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def _print_table(events: tuple[NewsEvent, ...]) -> None:
    rows = [
        (_format_utc(event.time_utc), event.currency, event.impact, event.title) for event in events
    ]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(OUTPUT_COLUMNS)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(OUTPUT_COLUMNS)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _print_counts(events: tuple[NewsEvent, ...]) -> None:
    currencies = Counter(event.currency for event in events)
    impacts = Counter(event.impact for event in events)
    print(
        "Count by currency: "
        + (", ".join(f"{key}={value}" for key, value in sorted(currencies.items())) or "none")
    )
    print(
        "Count by impact: "
        + (", ".join(f"{key}={value}" for key, value in sorted(impacts.items())) or "none")
    )
    if events:
        first = events[0].time_utc
        last = events[-1].time_utc
        days_covered = (last.date() - first.date()).days + 1
        print(f"First event: {_format_utc(first)}")
        print(f"Last event: {_format_utc(last)}")
        print(f"Days covered (inclusive UTC dates): {days_covered}")
    else:
        print("First event: none")
        print("Last event: none")
        print("Days covered (inclusive UTC dates): 0")


def _parse_impact_filter(value: str) -> frozenset[str]:
    impacts = frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    invalid = impacts - ALLOWED_IMPACTS
    if not impacts or invalid:
        choices = ", ".join(sorted(invalid)) if invalid else "(empty)"
        raise ValueError(f"Invalid --impact value(s): {choices}")
    return impacts


def merge_events(
    new_events: tuple[NewsEvent, ...],
    *,
    candidate_path: Path = CANDIDATE_CALENDAR,
    impacts: frozenset[str] = DEFAULT_IMPACTS,
) -> list[NewsEvent]:
    existing = load_news_calendar(candidate_path) if candidate_path.exists() else []
    merged: dict[tuple[datetime, str, str], NewsEvent] = {}
    for event in (*existing, *new_events):
        if event.impact not in impacts:
            continue
        merged.setdefault(_event_key(event), event)
    result = sorted(
        merged.values(),
        key=lambda event: (event.time_utc, event.currency, event.title),
    )
    _write_events(candidate_path, result)
    return result


def _load_min_days_covered(settings_path: Path | None = None) -> int:
    path = settings_path or PROJECT_ROOT / "settings.toml"
    settings, _ = load_settings(path)
    return settings.news.min_days_covered


def _validate_promoted_calendar(path: Path, *, now_utc: datetime, min_days: int) -> list[NewsEvent]:
    events = load_news_calendar(path)
    events = validate_news_calendar(path, min_days_covered=min_days, now_utc=now_utc)
    within_next_week = [
        event for event in events if now_utc <= event.time_utc <= now_utc + timedelta(days=7)
    ]
    if not within_next_week:
        raise ValueError("Calendar has no events in the next 7 days")
    return events


def promote_candidate(
    *,
    approved_path: Path = APPROVED_CALENDAR,
    candidate_path: Path = CANDIDATE_CALENDAR,
    backup_dir: Path = BACKUP_DIR,
    min_days: int,
    now_utc: datetime | None = None,
    confirmation: str,
) -> Path:
    current_time = now_utc or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current_time = current_time.astimezone(UTC)

    if confirmation != "yes":
        raise ValueError('Promotion cancelled: typed confirmation must be exactly "yes"')
    if not approved_path.exists():
        raise FileNotFoundError(f"Approved calendar not found: {approved_path}")
    _validate_promoted_calendar(candidate_path, now_utc=current_time, min_days=min_days)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (f"news-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.csv")
    shutil.copy2(approved_path, backup_path)

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=approved_path.parent,
            prefix=f".{approved_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
        shutil.copyfile(candidate_path, temporary_path)
        os.replace(temporary_path, approved_path)
        temporary_path = None
        _validate_promoted_calendar(
            approved_path,
            now_utc=current_time,
            min_days=min_days,
        )
    except OSError, ValueError:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
        restore_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=approved_path.parent,
                prefix=f".{approved_path.name}.restore.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                restore_path = temporary.name
            shutil.copyfile(backup_path, restore_path)
            os.replace(restore_path, approved_path)
        except OSError as restore_error:
            if restore_path and os.path.exists(restore_path):
                os.unlink(restore_path)
            raise OSError(
                f"Promotion failed and approved calendar restoration failed; "
                f"backup retained at {backup_path}"
            ) from restore_error
        raise
    return backup_path


def _show_candidate_diff(approved_path: Path, candidate_path: Path) -> None:
    old_rows = approved_path.read_text(encoding="utf-8").splitlines()[1:]
    new_rows = candidate_path.read_text(encoding="utf-8").splitlines()[1:]
    old_counts = Counter(old_rows)
    new_counts = Counter(new_rows)
    removed = list((old_counts - new_counts).elements())
    added = list((new_counts - old_counts).elements())
    print(f"Rows added: {len(added)}; rows removed: {len(removed)}")
    for row in removed:
        print(f"- {row}")
    for row in added:
        print(f"+ {row}")
    if not added and not removed:
        print("No row changes.")


def _check_command(args: argparse.Namespace) -> int:
    try:
        result = check_input(args.input, input_timezone=args.input_timezone)
    except InputValidationError as error:
        _print_warnings(tuple(error.warnings))
        for problem in error.problems:
            print(f"Error: {problem}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Input check failed: {error}", file=sys.stderr)
        return 2
    _print_warnings(result.warnings)
    print(f"Input is valid: {len(result.events)} event(s); no files written.")
    return 0


def _preview_command(args: argparse.Namespace) -> int:
    try:
        result = check_input(args.input, input_timezone=args.input_timezone)
    except InputValidationError as error:
        _print_warnings(tuple(error.warnings))
        for problem in error.problems:
            print(f"Error: {problem}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Preview failed: {error}", file=sys.stderr)
        return 2
    _print_warnings(result.warnings)
    _print_table(result.events)
    _print_counts(result.events)
    print("No files written.")
    return 0


def _merge_command(args: argparse.Namespace) -> int:
    try:
        impacts = _parse_impact_filter(args.impact)
        result = check_input(args.input, input_timezone=args.input_timezone)
        _print_warnings(result.warnings)
        merged = merge_events(result.events, impacts=impacts)
    except InputValidationError as error:
        _print_warnings(tuple(error.warnings))
        for problem in error.problems:
            print(f"Error: {problem}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Merge failed: {error}", file=sys.stderr)
        return 2
    print(f"Candidate updated: {CANDIDATE_CALENDAR}; {len(merged)} event(s).")
    _print_counts(tuple(merged))
    return 0


def _promote_command(_: argparse.Namespace) -> int:
    try:
        min_days = _load_min_days_covered()
        if not APPROVED_CALENDAR.exists():
            raise FileNotFoundError(f"Approved calendar not found: {APPROVED_CALENDAR}")
        if not CANDIDATE_CALENDAR.exists():
            raise FileNotFoundError(f"Candidate calendar not found: {CANDIDATE_CALENDAR}")
        _show_candidate_diff(APPROVED_CALENDAR, CANDIDATE_CALENDAR)
        confirmation = input('Type "yes" to promote this candidate: ')
        backup = promote_candidate(
            min_days=min_days,
            confirmation=confirmation,
        )
    except (EOFError, OSError, ValueError) as error:
        print(f"Promotion refused: {error}", file=sys.stderr)
        return 2
    print(f"Candidate promoted. Previous approved calendar backed up to {backup}.")
    return 0


def _coverage_command(_: argparse.Namespace) -> int:
    try:
        min_days = _load_min_days_covered()
        events = load_news_calendar(APPROVED_CALENDAR)
    except (OSError, ValueError) as error:
        print(f"Coverage check failed: {error}", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    last_event = max((event.time_utc for event in events), default=None)
    future_events = [event for event in events if event.time_utc >= now]
    days_ahead = (
        max(0.0, (max(event.time_utc for event in future_events) - now).total_seconds() / 86400)
        if future_events
        else 0.0
    )
    meets_coverage = days_ahead >= min_days
    try:
        validate_news_calendar(
            APPROVED_CALENDAR,
            min_days_covered=min_days,
            now_utc=now,
        )
    except ValueError as error:
        meets_coverage = False
        print(f"Calendar validation: failed ({error})")
    else:
        print("Calendar validation: passed")
    print(f"Coverage ahead: {days_ahead:.1f} days")
    print(f"Last event date: {last_event.date().isoformat() if last_event else 'none'}")
    print(f"Meets min_days_covered ({min_days}): {'yes' if meets_coverage else 'no'}")
    return 0 if meets_coverage else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("template", help="write the manual-entry CSV template")
    for name in ("check", "preview", "merge"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--input-timezone", default="UTC")
        if name == "merge":
            command.add_argument("--impact", default="high,medium")

    commands.add_parser("promote", help="review and promote the candidate calendar")
    commands.add_parser("coverage", help="check the approved calendar's coverage")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        try:
            write_template()
        except OSError as error:
            print(f"Template creation failed: {error}", file=sys.stderr)
            return 2
        print(f"Template written to {TEMPLATE_CALENDAR}")
        return 0
    if args.command == "check":
        return _check_command(args)
    if args.command == "preview":
        return _preview_command(args)
    if args.command == "merge":
        return _merge_command(args)
    if args.command == "promote":
        return _promote_command(args)
    return _coverage_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
