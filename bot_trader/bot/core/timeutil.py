from __future__ import annotations

import math
import warnings
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
NEW_YORK_SERVER_OFFSET_HOURS = 7


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def expected_new_york_offset(utc_time: datetime) -> int:
    """Return New York local time plus seven hours for the given UTC instant."""
    localized = _require_utc(utc_time).astimezone(NEW_YORK)
    offset = localized.utcoffset()
    if offset is None:
        raise ValueError("New York UTC offset is unavailable")
    return int(offset.total_seconds() // 3_600) + NEW_YORK_SERVER_OFFSET_HOURS


def rounded_server_offset_from_tick(tick_time_msc: int, utc_time: datetime) -> float:
    if isinstance(tick_time_msc, bool) or not isinstance(tick_time_msc, int) or tick_time_msc <= 0:
        raise ValueError("tick_time_msc must be a positive integer")
    now = _require_utc(utc_time)
    offset_hours = tick_time_msc / 1_000 - now.timestamp()
    return math.floor(offset_hours / 1_800 + 0.5) / 2


def broker_wall_time_to_utc(server_epoch: float) -> datetime:
    """Convert MT5's server wall-clock epoch using the New York DST rule."""
    if (
        isinstance(server_epoch, bool)
        or not isinstance(server_epoch, (int, float))
        or not math.isfinite(server_epoch)
    ):
        raise ValueError("server_epoch must be a finite number")
    server_wall_time = datetime.fromtimestamp(server_epoch, tz=UTC).replace(tzinfo=None)
    candidates = []
    for offset_hours in (2, 3):
        candidate = (
            server_wall_time - timedelta(hours=offset_hours)
        ).replace(tzinfo=UTC)
        if expected_new_york_offset(candidate) == offset_hours:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("Broker wall time does not match the New York DST rule")
    if len(candidates) > 1:
        raise ValueError("Broker wall time is ambiguous during a New York DST transition")
    return candidates[0]


def check_broker_offset(broker_offset_hours: int, utc_time: datetime) -> int:
    """Warn at startup if the broker offset does not match the expected New York rule."""
    expected_offset = expected_new_york_offset(utc_time)
    if broker_offset_hours != expected_offset:
        warnings.warn(
            (
                f"Broker offset {broker_offset_hours} does not match the expected "
                f"New York offset {expected_offset}; raise an alert and confirm the "
                "broker clock before trading."
            ),
            RuntimeWarning,
            stacklevel=2,
        )
    return expected_offset


def broker_to_utc(server_epoch: int, server_offset_hours: int) -> datetime:
    """Convert a broker wall-clock epoch using the offset reported by the broker."""
    server_wall_time = datetime.fromtimestamp(server_epoch, tz=UTC).replace(tzinfo=None)
    return (server_wall_time - timedelta(hours=server_offset_hours)).replace(tzinfo=UTC)


def utc_to_broker_wall_time(utc_time: datetime) -> datetime:
    """Encode a UTC instant as the broker's server wall time for MT5 date inputs."""
    at_utc = _require_utc(utc_time)
    server_wall_time = at_utc + timedelta(
        hours=expected_new_york_offset(at_utc)
    )
    return server_wall_time.replace(tzinfo=UTC)


def _server_midnight_to_utc(server_midnight: datetime) -> datetime:
    for offset_hours in (2, 3):
        candidate = (server_midnight - timedelta(hours=offset_hours)).replace(tzinfo=UTC)
        if expected_new_york_offset(candidate) == offset_hours:
            return candidate
    raise ValueError("Server midnight does not match the expected New York offset")


def trading_day_start_utc(utc_time: datetime, server_offset_hours: int) -> datetime:
    """Return the New York-rule server midnight that began this trading day."""
    at_utc = _require_utc(utc_time)
    server_date = (at_utc + timedelta(hours=server_offset_hours)).date()
    return _server_midnight_to_utc(datetime.combine(server_date, time.min))


def month_start_utc(utc_time: datetime, server_offset_hours: int) -> datetime:
    """Return the server midnight that began the current broker-time month."""
    at_utc = _require_utc(utc_time)
    server_time = at_utc + timedelta(hours=server_offset_hours)
    month_date = date(server_time.year, server_time.month, 1)
    return _server_midnight_to_utc(datetime.combine(month_date, time.min))


def next_trading_day_boundary_utc(utc_time: datetime, server_offset_hours: int) -> datetime:
    """Return the next server-midnight trading-day boundary."""
    at_utc = _require_utc(utc_time)
    server_date = (at_utc + timedelta(hours=server_offset_hours)).date() + timedelta(days=1)
    return _server_midnight_to_utc(datetime.combine(server_date, time.min))


def next_rollover_utc(utc_time: datetime, server_offset_hours: int) -> datetime:
    """Return the next UTC instant of broker-server midnight."""
    return next_trading_day_boundary_utc(utc_time, server_offset_hours)


def next_month_boundary_utc(utc_time: datetime, server_offset_hours: int) -> datetime:
    """Return the next server-midnight month boundary."""
    at_utc = _require_utc(utc_time)
    server_time = at_utc + timedelta(hours=server_offset_hours)
    if server_time.month == 12:
        next_month = date(server_time.year + 1, 1, 1)
    else:
        next_month = date(server_time.year, server_time.month + 1, 1)
    return _server_midnight_to_utc(datetime.combine(next_month, time.min))
