from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Protocol, cast

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.data import Timeframe
from bot.core.timeutil import broker_wall_time_to_utc, expected_new_york_offset

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
    approved_by: str | None
    approved_on: str | None

    @property
    def approved(self) -> bool:
        return bool(self.approved_by and self.approved_on)

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


@dataclass(frozen=True, slots=True)
class KnownHolidayWindow:
    start_utc: datetime
    end_utc: datetime
    reason: str
    approved_by: str | None
    approved_on: str | None

    @property
    def approved(self) -> bool:
        return bool(self.approved_by and self.approved_on)

    def as_dict(self) -> dict[str, object]:
        return {
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
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
    category: str = "unexpected"
    source_category: str | None = None
    holiday_start_utc: datetime | None = None
    holiday_end_utc: datetime | None = None

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
            "category": self.category,
            "source_category": self.source_category,
            "holiday_start_utc": (
                self.holiday_start_utc.isoformat() if self.holiday_start_utc else None
            ),
            "holiday_end_utc": (
                self.holiday_end_utc.isoformat() if self.holiday_end_utc else None
            ),
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
                approved_by = _optional_text(row, "approved_by")
                approved_on = _optional_text(row, "approved_on")
                if bool(approved_by) != bool(approved_on):
                    raise ValueError("approved_by and approved_on must both be set or empty")
                if approved_on:
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
                        approved_by=approved_by,
                        approved_on=approved_on,
                    )
                )
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid known gap on CSV row {line_number}: {error}") from error
    return tuple(known_gaps)


def load_holiday_windows(path: str | Path) -> tuple[KnownHolidayWindow, ...]:
    windows: list[KnownHolidayWindow] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        rows = (line for line in file if not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        if tuple(reader.fieldnames or ()) != (
            "start_utc",
            "end_utc",
            "reason",
            "approved_by",
            "approved_on",
        ):
            raise ValueError("broker_holidays.csv has an invalid header")
        for line_number, row in enumerate(reader, start=2):
            try:
                start = _parse_utc(_required_text(row, "start_utc"))
                end = _parse_utc(_required_text(row, "end_utc"))
                reason = _required_text(row, "reason")
                approved_by = _optional_text(row, "approved_by")
                approved_on = _optional_text(row, "approved_on")
                if end <= start:
                    raise ValueError("holiday end must be after its start")
                if bool(approved_by) != bool(approved_on):
                    raise ValueError("approved_by and approved_on must both be set or empty")
                if approved_on:
                    date.fromisoformat(approved_on)
                windows.append(
                    KnownHolidayWindow(start, end, reason, approved_by, approved_on)
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid broker holiday on CSV row {line_number}: {error}"
                ) from error
    return tuple(windows)


def inspect_history_gaps(
    candles: tuple[_HistoryCandle, ...],
    symbol: str,
    timeframe: Timeframe,
    sessions: BrokerSessionHoursServerTime,
    known_gaps: tuple[KnownHistoryGap, ...] = (),
    *,
    holiday_windows: tuple[KnownHolidayWindow, ...] = (),
) -> tuple[tuple[HistoryGap, ...], int]:
    """Return missing scheduled-bar runs and the count of expected closure intervals."""
    interval = _INTERVAL[timeframe]
    gaps: list[HistoryGap] = []
    expected_closures = 0
    for previous, current in pairwise(candles):
        previous_server = _utc_to_server_wall(previous.time)
        current_server = _utc_to_server_wall(current.time)
        difference = current_server - previous_server
        if difference <= interval:
            continue
        missing_times: list[datetime] = []
        cursor = previous_server + interval
        while cursor < current_server:
            if _server_wall_is_scheduled(cursor, interval, sessions):
                missing_times.append(
                    _server_wall_to_utc_between(cursor, previous.time, current.time)
                )
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
                        holiday_windows,
                    )
                )
                run.clear()
            run.append(missing_time)
        if run:
            gaps.append(
                _classify_gap(
                    symbol,
                    timeframe,
                    tuple(run),
                    interval,
                    known_gaps,
                    holiday_windows,
                )
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
    holiday_windows: tuple[KnownHolidayWindow, ...],
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
    holiday = next(
        (
            window
            for window in holiday_windows
            if window.start_utc <= start and end <= window.end_utc
        ),
        None,
    )
    if holiday is not None:
        return HistoryGap(
            symbol,
            timeframe,
            start,
            end,
            len(missing_times),
            weekdays,
            known=True,
            accepted=holiday.approved,
            reason=None if holiday.approved else "holiday window is DRAFT",
            approval_reason=holiday.reason,
            approved_by=holiday.approved_by,
            approved_on=holiday.approved_on,
            category="holiday window",
            source_category="holiday window",
            holiday_start_utc=holiday.start_utc,
            holiday_end_utc=holiday.end_utc,
        )
    if approval is not None:
        midweek = all(value.weekday() in (1, 2, 3) for value in missing_times)
        approved = approval.approved and (
            len(missing_times) == 1 or (len(missing_times) == 2 and midweek)
        )
        return HistoryGap(
            symbol,
            timeframe,
            start,
            end,
            len(missing_times),
            weekdays,
            known=True,
            accepted=approved,
            reason=None if approved else "known gap is DRAFT or is not a single candle",
            approval_reason=approval.reason,
            approved_by=approval.approved_by,
            approved_on=approval.approved_on,
            category="known single gap",
            source_category="known single gap",
        )
    if _is_clock_change_weekend(start):
        return HistoryGap(
            symbol,
            timeframe,
            start,
            end,
            len(missing_times),
            weekdays,
            reason="unexplained gap near US clock-change Sunday",
            category="clock-change weekend",
        )
    return HistoryGap(symbol, timeframe, start, end, len(missing_times), weekdays)


def _server_wall_to_utc(value: datetime) -> datetime:
    return broker_wall_time_to_utc(value.replace(tzinfo=UTC).timestamp())


def _utc_to_server_wall(value: datetime) -> datetime:
    offset = expected_new_york_offset(value)
    return (value + timedelta(hours=offset)).replace(tzinfo=None)


def _server_wall_is_scheduled(
    start: datetime,
    interval: timedelta,
    sessions: BrokerSessionHoursServerTime,
) -> bool:
    day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    for day_offset in (-1, 0):
        server_date = (start + timedelta(days=day_offset)).date()
        day_start = datetime.combine(server_date, time.min)
        day_end = day_start + timedelta(days=1)
        for raw_window in getattr(sessions, day_names[server_date.weekday()]):
            start_text, end_text = raw_window.split("-", maxsplit=1)
            session_start = day_start.replace(
                hour=time.fromisoformat(start_text).hour,
                minute=time.fromisoformat(start_text).minute,
            )
            session_end = (
                day_end
                if end_text == "24:00"
                else day_start.replace(
                    hour=time.fromisoformat(end_text).hour,
                    minute=time.fromisoformat(end_text).minute,
                )
            )
            if start < session_end and start + interval > session_start:
                return True
    return False


def _server_wall_to_utc_between(
    value: datetime,
    first_utc: datetime,
    last_utc: datetime,
) -> datetime:
    wall_utc = value.replace(tzinfo=UTC)
    candidates = tuple(
        (wall_utc - timedelta(hours=offset)).replace(tzinfo=UTC)
        for offset in (2, 3)
        if expected_new_york_offset(
            (wall_utc - timedelta(hours=offset)).replace(tzinfo=UTC)
        )
        == offset
    )
    in_range = tuple(candidate for candidate in candidates if first_utc < candidate < last_utc)
    if len(in_range) == 1:
        return in_range[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"Ambiguous server-wall gap time: {value.isoformat()}")


def _is_clock_change_weekend(value: datetime) -> bool:
    year = value.year
    second_sunday_march = _nth_weekday(year, 3, 6, 2)
    first_sunday_november = _nth_weekday(year, 11, 6, 1)
    return value.date() in (second_sunday_march, first_sunday_november)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    first_match = first + timedelta(days=(weekday - first.weekday()) % 7)
    return first_match + timedelta(weeks=occurrence - 1)


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


def _optional_text(row: dict[str, str | None], name: str) -> str | None:
    value = row.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None
