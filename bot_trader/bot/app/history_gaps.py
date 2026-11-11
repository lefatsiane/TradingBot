from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Protocol, cast

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.data import Timeframe
from bot.core.timeutil import broker_wall_time_to_utc

_INTERVAL = {"H1": timedelta(hours=1), "H4": timedelta(hours=4)}
_CSV_FIELDS = (
    "symbol",
    "timeframe",
    "start_utc",
    "end_utc",
    "missing_candles",
    "reason",
    "approved_by",
    "approved_on",
)


@dataclass(frozen=True, slots=True)
class KnownHistoryGap:
    symbol: str
    timeframe: Timeframe
    start_utc: datetime
    end_utc: datetime
    missing_candles: int
    reason: str
    approved_by: str
    approved_on: str

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "missing_candles": self.missing_candles,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
        }


class _HistoryCandle(Protocol):
    time: datetime


@dataclass(frozen=True, slots=True)
class HistoryGap:
    symbol: str
    timeframe: Timeframe
    start_utc: datetime
    end_utc: datetime
    missing_candles: int
    weekdays: tuple[str, ...]
    known: bool = False
    accepted: bool = False
    reason: str | None = None
    approval_reason: str | None = None
    approved_by: str | None = None
    approved_on: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "missing_candles": self.missing_candles,
            "weekdays": list(self.weekdays),
            "known": self.known,
            "accepted": self.accepted,
            "reason": self.reason,
            "approval_reason": self.approval_reason,
            "approved_by": self.approved_by,
            "approved_on": self.approved_on,
        }


def load_known_gaps(path: str | Path) -> tuple[KnownHistoryGap, ...]:
    known_gaps: list[KnownHistoryGap] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        rows = (line for line in file if not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        if tuple(reader.fieldnames or ()) != _CSV_FIELDS:
            raise ValueError("known_gaps.csv has an invalid header")
        for line_number, row in enumerate(reader, start=2):
            try:
                symbol = _required_text(row, "symbol")
                timeframe = _required_text(row, "timeframe")
                if timeframe not in _INTERVAL:
                    raise ValueError("timeframe must be H1 or H4")
                start = _parse_utc(_required_text(row, "start_utc"))
                end = _parse_utc(_required_text(row, "end_utc"))
                missing = int(_required_text(row, "missing_candles"))
                approved_on = _required_text(row, "approved_on")
                date.fromisoformat(approved_on)
                if end <= start or missing < 1:
                    raise ValueError("gap end and missing_candles must be positive")
                known_gaps.append(
                    KnownHistoryGap(
                        symbol=symbol,
                        timeframe=cast(Timeframe, timeframe),
                        start_utc=start,
                        end_utc=end,
                        missing_candles=missing,
                        reason=_required_text(row, "reason"),
                        approved_by=_required_text(row, "approved_by"),
                        approved_on=approved_on,
                    )
                )
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid known gap on CSV row {line_number}: {error}") from error
    return tuple(known_gaps)


def inspect_history_gaps(
    candles: tuple[_HistoryCandle, ...],
    symbol: str,
    timeframe: Timeframe,
    sessions: BrokerSessionHoursServerTime,
    known_gaps: tuple[KnownHistoryGap, ...] = (),
) -> tuple[tuple[HistoryGap, ...], int]:
    """Return missing scheduled-bar runs and the count of expected closure intervals."""
    interval = _INTERVAL[timeframe]
    gaps: list[HistoryGap] = []
    expected_closures = 0
    sessions_cache: dict[date, tuple[tuple[datetime, datetime], ...]] = {}
    for previous, current in pairwise(candles):
        difference = current.time - previous.time
        if difference <= interval:
            continue
        missing_times: list[datetime] = []
        cursor = previous.time + interval
        while cursor < current.time:
            if _time_is_scheduled(cursor, interval, sessions, sessions_cache):
                missing_times.append(cursor)
            cursor += interval
        if not missing_times:
            expected_closures += 1
            continue
        run: list[datetime] = []
        for missing_time in missing_times:
            if run and missing_time != run[-1] + interval:
                gaps.append(
                    _classify_gap(
                        symbol,
                        timeframe,
                        tuple(run),
                        interval,
                        known_gaps,
                    )
                )
                run.clear()
            run.append(missing_time)
        if run:
            gaps.append(
                _classify_gap(symbol, timeframe, tuple(run), interval, known_gaps)
            )
    return tuple(gaps), expected_closures


def candle_is_in_session(
    candle: _HistoryCandle,
    timeframe: Timeframe,
    sessions: BrokerSessionHoursServerTime,
) -> bool:
    interval = _INTERVAL[timeframe]
    return _time_is_scheduled(candle.time, interval, sessions, {})


def session_intervals(
    sessions: BrokerSessionHoursServerTime,
    first_utc: datetime,
    last_utc: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    intervals: list[tuple[datetime, datetime]] = []
    server_date = (first_utc - timedelta(days=1)).date()
    last_server_date = (last_utc + timedelta(days=1)).date()
    values = sessions.model_dump()
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    while server_date <= last_server_date:
        for raw_window in values[names[server_date.weekday()]]:
            start_text, end_text = raw_window.split("-", maxsplit=1)
            start_wall = datetime.combine(server_date, time.fromisoformat(start_text))
            end_date = server_date + timedelta(days=end_text == "24:00")
            end_time = time.min if end_text == "24:00" else time.fromisoformat(end_text)
            start = _server_wall_to_utc(start_wall)
            end = _server_wall_to_utc(datetime.combine(end_date, end_time))
            if end > first_utc and start < last_utc:
                intervals.append((start, end))
        server_date += timedelta(days=1)
    return tuple(sorted(intervals))


def _time_is_scheduled(
    start: datetime,
    interval: timedelta,
    sessions: BrokerSessionHoursServerTime,
    cache: dict[date, tuple[tuple[datetime, datetime], ...]],
) -> bool:
    for date_offset in (-1, 0, 1):
        day = start.date() + timedelta(days=date_offset)
        if day not in cache:
            cache[day] = session_intervals(
                sessions,
                datetime.combine(day, time.min, tzinfo=UTC),
                datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC),
            )
        if any(
            start < end and start + interval > beginning
            for beginning, end in cache[day]
        ):
            return True
    return False


def _classify_gap(
    symbol: str,
    timeframe: Timeframe,
    missing_times: tuple[datetime, ...],
    interval: timedelta,
    known_gaps: tuple[KnownHistoryGap, ...],
) -> HistoryGap:
    start = missing_times[0]
    end = missing_times[-1] + interval
    weekdays = tuple(dict.fromkeys(value.strftime("%A") for value in missing_times))
    approval = next(
        (
            gap for gap in known_gaps
            if (
                gap.symbol == symbol
                and gap.timeframe == timeframe
                and gap.start_utc == start
                and gap.end_utc == end
                and gap.missing_candles == len(missing_times)
            )
        ),
        None,
    )
    if approval is None:
        return HistoryGap(symbol, timeframe, start, end, len(missing_times), weekdays)
    midweek = all(value.weekday() in (1, 2, 3) for value in missing_times)
    accepted = len(missing_times) <= 2 and midweek
    reason = None if accepted else (
        "approved gap exceeds two candles or is not mid-week"
    )
    return HistoryGap(
        symbol,
        timeframe,
        start,
        end,
        len(missing_times),
        weekdays,
        known=True,
        accepted=accepted,
        reason=reason,
        approval_reason=approval.reason,
        approved_by=approval.approved_by,
        approved_on=approval.approved_on,
    )


def _server_wall_to_utc(value: datetime) -> datetime:
    return broker_wall_time_to_utc(value.replace(tzinfo=UTC).timestamp())


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def _required_text(row: dict[str, str | None], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()
