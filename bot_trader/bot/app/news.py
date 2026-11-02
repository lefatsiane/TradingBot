from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

PAIR_CURRENCIES: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "XAUUSD": ("XAU", "USD"),
}


@dataclass(frozen=True, slots=True)
class NewsEvent:
    time_utc: datetime
    currency: str
    impact: str
    title: str

    @classmethod
    def from_row(cls, row: dict[str, str], *, row_number: int) -> NewsEvent:
        time_value = (row.get("time_utc") or "").strip()
        currency = (row.get("currency") or "").strip().upper()
        impact = (row.get("impact") or "").strip().lower()
        title = (row.get("title") or "").strip()

        if not time_value:
            raise ValueError(f"Missing time_utc on row {row_number}")
        if not currency:
            raise ValueError(f"Missing currency on row {row_number}")
        if not impact:
            raise ValueError(f"Missing impact on row {row_number}")
        if not title:
            raise ValueError(f"Missing title on row {row_number}")

        try:
            time_utc = datetime.fromisoformat(time_value)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO timestamp on row {row_number}: {time_value!r}") from exc

        if time_utc.tzinfo is None:
            time_utc = time_utc.replace(tzinfo=UTC)
        time_utc = time_utc.astimezone(UTC)

        return cls(time_utc=time_utc, currency=currency, impact=impact, title=title)


def load_news_calendar(path: str | Path) -> list[NewsEvent]:
    calendar_path = Path(path)
    if not calendar_path.exists():
        raise FileNotFoundError(f"News calendar not found: {calendar_path}")

    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"News calendar is missing a header row: {calendar_path}")

        required = {"time_utc", "currency", "impact", "title"}
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError(f"News calendar missing required columns: {', '.join(missing)}")

        events: list[NewsEvent] = []
        for row_number, row in enumerate(reader, start=2):
            if row is None:
                continue
            if not any((value or "").strip() for value in row.values()):
                continue
            events.append(NewsEvent.from_row(row, row_number=row_number))

    return sorted(events, key=lambda event: event.time_utc)


def validate_news_calendar(path: str | Path, *, min_days_covered: int = 7) -> list[NewsEvent]:
    if min_days_covered < 1:
        raise ValueError("min_days_covered must be at least 1")

    events = load_news_calendar(path)
    if not events:
        raise ValueError("News calendar is empty")

    for previous, current in pairwise(events):
        if current.time_utc < previous.time_utc:
            raise ValueError("News calendar is not sorted by time_utc")
        gap = current.time_utc - previous.time_utc
        if gap > timedelta(days=7):
            raise ValueError("News calendar has a gap larger than one week")

    now_utc = datetime.now(UTC)
    future_events = [event for event in events if event.time_utc >= now_utc]
    if not future_events:
        raise ValueError("News calendar lacks future events")

    coverage = future_events[-1].time_utc - now_utc
    if coverage < timedelta(days=min_days_covered):
        raise ValueError(
            "News calendar does not cover at least "
            f"{min_days_covered} days ahead (coverage: {coverage.total_seconds() / 86400:.1f} days)"
        )

    return events


def affected_currencies_for_symbol(symbol: str) -> tuple[str, ...]:
    lookup = symbol.upper()
    if lookup not in PAIR_CURRENCIES:
        raise ValueError(f"Unsupported symbol for news gate: {symbol}")
    return PAIR_CURRENCIES[lookup]


def is_news_blocked(
    symbol: str,
    at_utc: datetime,
    events: Iterable[NewsEvent],
    *,
    before_min: int = 30,
    after_min: int = 30,
) -> bool:
    if before_min < 0 or after_min < 0:
        raise ValueError("News-block buffer minutes must be non-negative")

    if at_utc.tzinfo is None:
        at_utc = at_utc.replace(tzinfo=UTC)
    at_utc = at_utc.astimezone(UTC)
    currencies = set(affected_currencies_for_symbol(symbol))
    before_delta = timedelta(minutes=before_min)
    after_delta = timedelta(minutes=after_min)

    for event in events:
        if event.currency not in currencies:
            continue
        event_start = event.time_utc - before_delta
        event_end = event.time_utc + after_delta
        if event_start <= at_utc <= event_end:
            return True
    return False


def calendar_has_minimum_coverage(
    path: str | Path,
    *,
    now_utc: datetime | None = None,
    min_days_covered: int = 7,
) -> bool:
    try:
        events = validate_news_calendar(path, min_days_covered=min_days_covered)
    except ValueError:
        return False

    current_time = now_utc or datetime.now(UTC)
    future_events = [event for event in events if event.time_utc >= current_time]
    if not future_events:
        return False
    return (future_events[-1].time_utc - current_time) >= timedelta(days=min_days_covered)
