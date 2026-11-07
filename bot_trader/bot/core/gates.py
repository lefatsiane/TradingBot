from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from math import isfinite

from bot.core.types import ReasonCode


def next_weekend_close_utc(now_utc: datetime, sessions: Iterable[SessionWindow]) -> datetime:
    """Return the next Friday session close at or after now_utc."""
    current = _as_utc(now_utc, "now_utc")
    candidates: list[datetime] = []
    for session in sessions:
        crosses_midnight = session.full_day or session.closes_utc < session.opens_utc
        closing_weekday = (session.weekday + int(crosses_midnight)) % 7
        if session.weekday != 4 and closing_weekday != 4:
            continue
        closing_date = current.date() + timedelta(
            days=(closing_weekday - current.weekday()) % 7
        )
        closes = datetime.combine(closing_date, session.closes_utc, tzinfo=UTC)
        if closes < current:
            closes += timedelta(days=7)
        if closes >= current:
            candidates.append(closes)
    if not candidates:
        raise ValueError("No Friday session window available")
    return min(candidates)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    weekday: int
    opens_utc: time
    closes_utc: time
    full_day: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be in range 0..6")
        if self.opens_utc.tzinfo is not None or self.closes_utc.tzinfo is not None:
            raise ValueError("Session times must be naive UTC wall-clock times")
        if self.opens_utc == self.closes_utc and not self.full_day:
            raise ValueError("Session open and close times cannot be equal")
        if self.full_day and self.opens_utc != self.closes_utc:
            raise ValueError("Full-day sessions must have matching open and close times")


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: ReasonCode | None = None
    flatten_all: bool = False


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _within_session(at_utc: datetime, sessions: Iterable[SessionWindow]) -> bool:
    current_time = at_utc.time().replace(tzinfo=None)
    weekday = at_utc.weekday()
    for session in sessions:
        if session.opens_utc < session.closes_utc:
            if (
                weekday == session.weekday
                and session.opens_utc <= current_time < session.closes_utc
            ):
                return True
        elif (
            weekday == session.weekday
            and current_time >= session.opens_utc
        ) or (
            (weekday - 1) % 7 == session.weekday
            and current_time < session.closes_utc
        ):
            return True
    return False


def evaluate_entry_gates(
    *,
    at_utc: datetime,
    symbol_enabled: bool,
    sessions: Iterable[SessionWindow],
    spread_price: float,
    point: float,
    max_spread_points: float,
    candle_range: float,
    atr: float,
    max_candle_range_atr: float,
    rollover_at_utc: datetime,
    rollover_block_before_min: int,
    rollover_block_after_min: int,
    news_calendar_valid: bool,
    news_blocked: bool,
    weekend_close_utc: datetime,
    no_new_entries_before_weekend_close_min: int = 120,
    flatten_before_weekend_close_min: int = 30,
) -> GateDecision:
    now = _as_utc(at_utc, "at_utc")
    rollover = _as_utc(rollover_at_utc, "rollover_at_utc")
    weekend_close = _as_utc(weekend_close_utc, "weekend_close_utc")
    if weekend_close < now:
        raise ValueError("weekend_close_utc must be the next upcoming market close")
    numeric_inputs = (
        spread_price,
        point,
        max_spread_points,
        candle_range,
        atr,
        max_candle_range_atr,
    )
    if not all(isfinite(value) for value in numeric_inputs):
        raise ValueError("Gate inputs must be finite numbers")
    if min(spread_price, candle_range) < 0:
        raise ValueError("Spread and candle range cannot be negative")
    if min(point, atr, max_candle_range_atr) <= 0 or max_spread_points < 0:
        raise ValueError("Point, ATR and volatility limit must be positive; spread limit non-negative")
    if min(
        rollover_block_before_min,
        rollover_block_after_min,
        no_new_entries_before_weekend_close_min,
        flatten_before_weekend_close_min,
    ) < 0:
        raise ValueError("Gate window durations cannot be negative")
    if flatten_before_weekend_close_min > no_new_entries_before_weekend_close_min:
        raise ValueError("Weekend flatten window cannot exceed the no-entry window")

    if not symbol_enabled:
        return GateDecision(False, ReasonCode.SYMBOL_DISABLED)

    flatten_all = False
    seconds_to_close = (weekend_close - now).total_seconds()
    no_entry_window_s = no_new_entries_before_weekend_close_min * 60
    flatten_window_s = flatten_before_weekend_close_min * 60
    if seconds_to_close <= no_entry_window_s:
        flatten_all = seconds_to_close <= flatten_window_s
        return GateDecision(False, ReasonCode.WEEKEND_WINDOW, flatten_all)

    session_windows = tuple(sessions)
    if not _within_session(now, session_windows):
        return GateDecision(False, ReasonCode.OUTSIDE_HOURS)

    rollover_delta = (now - rollover).total_seconds()
    if (
        -rollover_block_before_min * 60
        <= rollover_delta
        <= rollover_block_after_min * 60
    ):
        return GateDecision(False, ReasonCode.ROLLOVER)

    if not news_calendar_valid:
        return GateDecision(False, ReasonCode.NEWS_CALENDAR_STALE)
    if news_blocked:
        return GateDecision(False, ReasonCode.NEWS_WINDOW)
    if spread_price > point * max_spread_points:
        return GateDecision(False, ReasonCode.SPREAD_WIDE)
    if candle_range > atr * max_candle_range_atr:
        return GateDecision(False, ReasonCode.SKIP_VOLATILITY)
    return GateDecision(True)
