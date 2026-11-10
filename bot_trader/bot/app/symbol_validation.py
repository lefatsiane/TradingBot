from __future__ import annotations

import json
import math
import os
import re
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import median

from bot.adapters.broker import Broker
from bot.app.config import (
    BrokerSessionHoursServerTime,
    ScheduleSeason,
    SymbolSettings,
    broker_session_hours_to_utc,
    parse_sessions,
    schedule_season_for_utc,
    session_windows_overlap,
)
from bot.app.data import DataQualityError, validate_candles
from bot.core.indicators import atr
from bot.core.sizing import size_trade
from bot.core.timeutil import broker_wall_time_to_utc, expected_new_york_offset
from bot.core.types import Candle, Proposal, Side

MIN_HISTORY_DAYS = 183
MAX_TICK_AGE = timedelta(minutes=10)
MAX_HISTORY_END_AGE = timedelta(days=5)
MAX_HISTORY_START_GAP = timedelta(days=3)
HISTORY_RETRY_DELAY_SECONDS = 1.0
MAX_TICK_FUTURE_SKEW = timedelta(seconds=30)
LIVE_SPREAD_SAMPLE_SECONDS = 60.0
LIVE_SPREAD_SAMPLE_INTERVAL_SECONDS = 5.0
ATR_PERIOD = 14
_H1 = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class SymbolValidationReport:
    symbol: str
    enabled: bool
    passed: bool
    history_bars: int
    spread_median_points: float | None
    spread_p95_points: float | None
    current_spread_points: float | None
    spread_limit_points: float
    suggested_hours_utc: tuple[str, ...]
    suggested_hours_utc_season: ScheduleSeason | None
    issues: tuple[str, ...]
    weekday_candle_times: tuple[WeekdayCandleTimes, ...] = ()
    last_closed_candle_age_hours: float | None = None
    tick_freshness: TickFreshnessDiagnostic | None = None
    data_gaps: tuple[datetime, ...] = ()
    live_spread_median_points: float | None = None
    live_spread_max_points: float | None = None
    live_spread_sample_count: int = 0


@dataclass(frozen=True, slots=True)
class TickFreshnessDiagnostic:
    raw_server_epoch: float | None
    converted_time_utc: datetime | None
    current_time_utc: datetime
    age_seconds: float | None
    threshold_seconds: float
    market_open: bool
    bid: float | None = None
    ask: float | None = None
    invalid_reason: str | None = None
    clock_skew_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LiveSpreadSamples:
    points: tuple[float, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WeekdayCandleTimes:
    weekday: str
    first_utc: datetime | None
    last_utc: datetime | None


def validate_symbol(
    broker: Broker,
    symbol: str,
    settings: SymbolSettings,
    *,
    equity: float,
    risk_per_trade_pct: float,
    cost_gate_pct_of_risk: float,
    max_stop_atr: float,
    now_utc: datetime,
    server_session_hours: BrokerSessionHoursServerTime,
    live_spread_samples: tuple[float, ...] | None = None,
    live_spread_errors: tuple[str, ...] = (),
) -> SymbolValidationReport:
    now = _require_utc(now_utc)
    if equity <= 0 or risk_per_trade_pct <= 0 or max_stop_atr <= 0:
        raise ValueError("Equity, risk percentage, and maximum stop ATR must be positive")

    issues: list[str] = []
    try:
        spec = broker.symbol_info(symbol)
    except (OSError, RuntimeError, ValueError) as error:
        return _report(
            symbol,
            settings.enabled,
            0,
            None,
            None,
            None,
            settings.max_spread_points,
            (),
            None,
            (f"symbol spec: {error}", *live_spread_errors),
        )

    tick_read_error: str | None = None
    try:
        tick = broker.tick(symbol)
    except (OSError, RuntimeError, ValueError) as error:
        tick = None
        tick_read_error = f"tick read failed: {error}"
    market_open = _market_is_open(now, server_session_hours)
    tick_invalid_reason = _tick_invalid_reason(tick, now)
    tick_valid = tick_invalid_reason is None
    tick_freshness = _tick_freshness_diagnostic(
        tick,
        now,
        market_open=market_open,
        invalid_reason=tick_invalid_reason,
    )
    if tick_read_error is not None and market_open:
        issues.append(tick_read_error)
    elif not tick_valid and market_open:
        issues.append(f"invalid tick while the market is open: {tick_invalid_reason}")

    cutoff = now - timedelta(days=MIN_HISTORY_DAYS)
    history_bars = 0
    recent: tuple[Candle, ...] = ()
    last_closed_candle_age_hours: float | None = None
    try:
        raw_candles = broker.candles(symbol, "H1")
        if (
            settings.enabled
            and market_open
            and _history_is_behind(raw_candles, now, server_session_hours)
        ):
            time_module.sleep(HISTORY_RETRY_DELAY_SECONDS)
            raw_candles = broker.candles(symbol, "H1")
    except (OSError, RuntimeError, ValueError) as error:
        issues.append(f"H1 history read failed: {error}")
    else:
        recent = tuple(
            candle
            for candle in raw_candles
            if cutoff <= candle.time and candle.time + _H1 <= now
        )
        history_bars = len(recent)
        if not recent:
            issues.append("no closed H1 history in the six-month validation window")
        else:
            try:
                validate_candles(recent, "H1")
            except DataQualityError as error:
                if not str(error).startswith("Unexpected H1 candle gap from "):
                    issues.append(f"H1 history quality: {error}")
            if _history_start_is_stale(recent[0].time, cutoff):
                issues.append("H1 history does not cover the full six-month validation window")
            last_closed = recent[-1].time + _H1
            last_closed_candle_age_hours = max(
                0.0,
                (now - last_closed).total_seconds() / 3_600,
            )
            expected_last_close = _latest_expected_candle_close(
                now,
                server_session_hours,
            )
            if (
                settings.enabled
                and market_open
                and expected_last_close is not None
                and last_closed < expected_last_close
            ):
                issues.append(
                    "H1 history is not up to date: last closed candle age "
                    f"{last_closed_candle_age_hours:.1f} hours; expected through "
                    f"{expected_last_close:%Y-%m-%d %H:%M UTC}"
                )
            market_open_gap = _market_open_duration(
                last_closed,
                now,
                server_session_hours,
            )
            if market_open_gap > MAX_HISTORY_END_AGE:
                issues.append(
                    "H1 history is stale: last closed candle age "
                    f"{last_closed_candle_age_hours:.1f} hours; market-open gap "
                    f"{market_open_gap.total_seconds() / 3_600:.1f} hours exceeds "
                    f"{MAX_HISTORY_END_AGE.total_seconds() / 3_600:.0f} hours"
                )

    current_spread_points: float | None = None
    if tick_valid and tick is not None:
        current_spread_points = (tick.ask - tick.bid) / spec.point

    spread_median_points, spread_p95_points = _historical_spread_statistics(
        recent,
        spec.point,
    )
    live_spreads = tuple(
        sample
        for sample in (live_spread_samples or ())
        if math.isfinite(sample) and sample >= 0
    )
    live_spread_median = median(live_spreads) if live_spreads else None
    live_spread_max = max(live_spreads) if live_spreads else None
    issues.extend(live_spread_errors)
    if live_spreads and live_spread_median is not None and (
        live_spread_median > settings.max_spread_points
    ):
        issues.append(
            f"live median spread {live_spread_median:.2f} points exceeds "
            f"max_spread_points {settings.max_spread_points:.2f}"
        )
    elif live_spread_samples is not None and market_open and not live_spreads:
        issues.append("no fresh live spread samples collected while the market is open")

    suggested_hours: tuple[str, ...] = ()
    suggested_season: ScheduleSeason | None = None
    weekday_candle_times = _weekday_candle_summaries(recent)
    if settings.enabled:
        try:
            suggested_season = schedule_season_for_utc(now)
            suggested_hours = broker_session_hours_to_utc(
                server_session_hours,
                now,
            )
            _validate_schedule(suggested_hours)
            outside, missing = _check_session_candles(
                recent,
                server_session_hours,
                cutoff=cutoff,
                end_utc=now,
            )
            if outside:
                issues.append(
                    f"{len(outside)} H1 candle(s) outside broker session hours; "
                    f"first: {_format_candle_times(outside)}"
                )
            if missing:
                data_gaps = missing
            else:
                data_gaps = ()
        except (OSError, RuntimeError, ValueError) as error:
            issues.append(f"server session validation failed: {error}")
            data_gaps = ()
    else:
        data_gaps = ()

    if recent and tick_valid and tick is not None:
        average_true_range = atr(recent, ATR_PERIOD)[-1]
        if average_true_range <= 0:
            issues.append("cannot calculate a positive H1 ATR for the minimum-lot check")
        else:
            entry = (tick.bid + tick.ask) / 2
            stop_distance = max_stop_atr * average_true_range
            proposal = Proposal(
                side=Side.LONG,
                entry=entry,
                stop_loss=entry - stop_distance,
                take_profit=entry + 2 * stop_distance,
                reason="SYMBOL_VALIDATION",
            )
            size = size_trade(
                equity,
                risk_per_trade_pct,
                proposal,
                spec,
                bid_price=tick.bid,
                ask_price=tick.ask,
                commission_per_lot_side=settings.commission_per_lot_side,
                slippage_price=settings.slippage_points * spec.point,
                cost_gate_pct_of_risk=cost_gate_pct_of_risk,
            )
            if size.reason is not None:
                issues.append(size.reason)

    return _report(
        symbol,
        settings.enabled,
        history_bars,
        spread_median_points,
        spread_p95_points,
        current_spread_points,
        settings.max_spread_points,
        suggested_hours,
        suggested_season,
        tuple(issues),
        weekday_candle_times,
        last_closed_candle_age_hours,
        tick_freshness,
        data_gaps,
        live_spread_median,
        live_spread_max,
        len(live_spreads),
    )


def sample_live_spread(
    broker: Broker,
    symbol: str,
    point: float,
    *,
    duration_seconds: float = LIVE_SPREAD_SAMPLE_SECONDS,
    interval_seconds: float = LIVE_SPREAD_SAMPLE_INTERVAL_SECONDS,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
    monotonic: Callable[[], float] = time_module.monotonic,
) -> LiveSpreadSamples:
    if point <= 0 or duration_seconds < 0 or interval_seconds <= 0:
        raise ValueError("Spread point and sampling durations must be positive")
    now_provider = (
        (lambda: datetime.now(tz=UTC))
        if clock is None
        else clock
    )
    started = monotonic()
    samples: list[float] = []
    errors: list[str] = []
    while True:
        now = _require_utc(now_provider())
        try:
            tick = broker.tick(symbol)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(f"live spread tick read failed ({type(error).__name__})")
            tick = None
        if tick is not None and _tick_invalid_reason(tick, now) is None:
            samples.append((tick.ask - tick.bid) / point)
        elapsed = monotonic() - started
        remaining = duration_seconds - elapsed
        if remaining <= 0:
            break
        sleep(min(interval_seconds, remaining))
    return LiveSpreadSamples(tuple(samples), tuple(dict.fromkeys(errors)))


def _weekday_candle_summaries(
    candles: tuple[Candle, ...],
) -> tuple[WeekdayCandleTimes, ...]:
    summaries: list[WeekdayCandleTimes] = []
    for weekday, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        seen = [candle.time for candle in candles if candle.time.weekday() == weekday]
        summaries.append(
            WeekdayCandleTimes(
                weekday=name,
                first_utc=seen[0] if seen else None,
                last_utc=seen[-1] if seen else None,
            )
        )
    return tuple(summaries)


def _check_session_candles(
    candles: tuple[Candle, ...],
    server_hours: BrokerSessionHoursServerTime,
    *,
    cutoff: datetime,
    end_utc: datetime,
) -> tuple[tuple[datetime, ...], tuple[datetime, ...]]:
    if not candles:
        return (), ()
    intervals = _server_session_intervals(
        server_hours,
        candles[0].time.date() - timedelta(days=1),
        candles[-1].time.date() + timedelta(days=1),
    )
    candle_times = {candle.time for candle in candles}
    outside: list[datetime] = []
    interval_index = 0
    for candle in candles:
        while interval_index < len(intervals) and intervals[interval_index][1] <= candle.time:
            interval_index += 1
        candle_end = candle.time + _H1
        if interval_index == len(intervals) or not (
            candle.time < intervals[interval_index][1]
            and candle_end > intervals[interval_index][0]
        ):
            outside.append(candle.time)
    missing: list[datetime] = []
    for start, end in intervals:
        expected = start.replace(minute=0, second=0, microsecond=0)
        while expected < end:
            if (
                cutoff <= expected
                and expected + _H1 <= end_utc
                and expected + _H1 > start
                and expected not in candle_times
            ):
                missing.append(expected)
            expected += _H1
    return tuple(outside), tuple(missing)


def _server_session_intervals(
    server_hours: BrokerSessionHoursServerTime,
    first_server_date: date,
    last_server_date: date,
) -> tuple[tuple[datetime, datetime], ...]:
    intervals: list[tuple[datetime, datetime]] = []
    session_values = server_hours.model_dump()
    current_date = first_server_date
    while current_date <= last_server_date:
        day_name = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[
            current_date.weekday()
        ]
        for window in session_values[day_name]:
            start_text, end_text = window.split("-", maxsplit=1)
            start_wall = datetime.combine(current_date, time.fromisoformat(start_text))
            end_date = current_date + timedelta(days=end_text == "24:00")
            end_time = time.min if end_text == "24:00" else time.fromisoformat(end_text)
            end_wall = datetime.combine(end_date, end_time)
            start_utc = _server_wall_datetime_to_utc(start_wall)
            end_utc = _server_wall_datetime_to_utc(end_wall)
            if end_utc <= start_utc:
                raise ValueError(f"Invalid converted server session on {current_date}")
            intervals.append((start_utc, end_utc))
        current_date += timedelta(days=1)
    return tuple(sorted(intervals))


def _server_wall_datetime_to_utc(server_wall: datetime) -> datetime:
    server_epoch = server_wall.replace(tzinfo=UTC).timestamp()
    return broker_wall_time_to_utc(server_epoch)


def _latest_expected_candle_close(
    now_utc: datetime,
    server_hours: BrokerSessionHoursServerTime,
) -> datetime | None:
    first_date = now_utc.date() - timedelta(days=8)
    intervals = _server_session_intervals(
        server_hours,
        first_date,
        now_utc.date() + timedelta(days=1),
    )
    latest_close: datetime | None = None
    for session_start, session_end in intervals:
        candle_start = session_start.replace(minute=0, second=0, microsecond=0)
        while candle_start < session_end:
            candle_close = candle_start + _H1
            if (
                candle_start < session_end
                and candle_close > session_start
                and candle_close <= now_utc
                and (latest_close is None or candle_close > latest_close)
            ):
                latest_close = candle_close
            candle_start += _H1
    return latest_close


def _history_is_behind(
    candles: tuple[Candle, ...],
    now_utc: datetime,
    server_hours: BrokerSessionHoursServerTime,
) -> bool:
    expected_close = _latest_expected_candle_close(now_utc, server_hours)
    if expected_close is None:
        return False
    closed = tuple(candle.time + _H1 for candle in candles if candle.time + _H1 <= now_utc)
    return not closed or max(closed) < expected_close


def _market_is_open(
    at_utc: datetime,
    server_hours: BrokerSessionHoursServerTime,
) -> bool:
    server_date = (
        at_utc + timedelta(hours=expected_new_york_offset(at_utc))
    ).date()
    intervals = _server_session_intervals(
        server_hours,
        server_date - timedelta(days=1),
        server_date + timedelta(days=1),
    )
    return any(start <= at_utc < end for start, end in intervals)


def _market_open_duration(
    start_utc: datetime,
    end_utc: datetime,
    server_hours: BrokerSessionHoursServerTime,
) -> timedelta:
    if end_utc <= start_utc:
        return timedelta(0)
    intervals = _server_session_intervals(
        server_hours,
        start_utc.date() - timedelta(days=1),
        end_utc.date() + timedelta(days=1),
    )
    total = timedelta(0)
    for session_start, session_end in intervals:
        overlap_start = max(start_utc, session_start)
        overlap_end = min(end_utc, session_end)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def _is_excluded_spread_candle(candle_time_utc: datetime) -> bool:
    server_time = candle_time_utc + timedelta(
        hours=expected_new_york_offset(candle_time_utc)
    )
    first_week_hour = server_time.weekday() == 6 and server_time.hour == 0
    last_week_hour = server_time.weekday() == 4 and server_time.hour == 23
    rollover = server_time.hour >= 23 or server_time.hour < 1
    return rollover or first_week_hour or last_week_hour


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _historical_spread_statistics(
    candles: tuple[Candle, ...],
    point: float,
) -> tuple[float | None, float | None]:
    spreads = [
        candle.spread / point
        for candle in candles
        if not _is_excluded_spread_candle(candle.time)
    ]
    return (
        median(spreads) if spreads else None,
        _percentile_95(spreads),
    )


def _format_optional_points(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _history_start_is_stale(first_candle: datetime, cutoff: datetime) -> bool:
    return first_candle > cutoff + MAX_HISTORY_START_GAP


def _tick_freshness_diagnostic(
    tick: object | None,
    now: datetime,
    *,
    market_open: bool,
    invalid_reason: str | None,
) -> TickFreshnessDiagnostic:
    tick_time = getattr(tick, "time_utc", None)
    valid_time = (
        tick_time
        if isinstance(tick_time, datetime)
        and tick_time.tzinfo is not None
        and tick_time.utcoffset() == timedelta(0)
        else None
    )
    age_seconds = (
        (now - valid_time.astimezone(UTC)).total_seconds()
        if valid_time is not None
        else None
    )
    raw_server_epoch = getattr(tick, "time_server_epoch", None)
    if not isinstance(raw_server_epoch, (int, float)) or isinstance(raw_server_epoch, bool):
        raw_server_epoch = None
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    if not isinstance(bid, (int, float)) or isinstance(bid, bool):
        bid = None
    if not isinstance(ask, (int, float)) or isinstance(ask, bool):
        ask = None
    return TickFreshnessDiagnostic(
        raw_server_epoch=raw_server_epoch,
        converted_time_utc=valid_time,
        current_time_utc=now,
        age_seconds=age_seconds,
        threshold_seconds=MAX_TICK_AGE.total_seconds(),
        market_open=market_open,
        bid=float(bid) if bid is not None else None,
        ask=float(ask) if ask is not None else None,
        invalid_reason=invalid_reason,
        clock_skew_seconds=max(0.0, -age_seconds) if age_seconds is not None else None,
    )


def _format_candle_times(candles: tuple[datetime, ...]) -> str:
    examples = ", ".join(
        candle_time.strftime("%Y-%m-%d %H:%M UTC")
        for candle_time in candles[:5]
    )
    remaining = len(candles) - min(len(candles), 5)
    suffix = f", plus {remaining} more" if remaining else ""
    return examples + suffix


def write_hours_utc(
    settings_path: str | Path,
    schedules: dict[str, tuple[str, ...]],
    season: ScheduleSeason,
) -> dict[str, tuple[str, ...]]:
    path = Path(settings_path)
    if not schedules:
        return {}
    with path.open("r", encoding="utf-8", newline="") as settings_file:
        lines = settings_file.read().splitlines(keepends=True)

    def content(line: str) -> str:
        return line.rstrip("\r\n")

    changed_lines: dict[str, tuple[str, ...]] = {}
    for symbol, hours in schedules.items():
        if not hours:
            raise ValueError(f"Cannot write an empty UTC schedule for {symbol}")
        _validate_schedule(hours)
        section_header = re.compile(rf"^\s*\[symbols\.{re.escape(symbol)}\]\s*(?:#.*)?$")
        section_indices = [
            index
            for index, line in enumerate(lines)
            if section_header.fullmatch(content(line))
        ]
        if len(section_indices) != 1:
            raise ValueError(f"Expected exactly one [symbols.{symbol}] section")
        start = section_indices[0] + 1
        end = next(
            (
                index
                for index in range(start, len(lines))
                if content(lines[index]).lstrip().startswith("[")
            ),
            len(lines),
        )
        hours_indices = [
            index
            for index in range(start, end)
            if re.match(r"^\s*hours_utc\s*=", content(lines[index]))
        ]
        if len(hours_indices) != 1:
            raise ValueError(f"Expected exactly one hours_utc setting for {symbol}")
        season_indices = [
            index
            for index in range(start, end)
            if re.match(r"^\s*hours_utc_season\s*=", content(lines[index]))
        ]
        if len(season_indices) > 1:
            raise ValueError(
                f"Expected at most one hours_utc_season setting for {symbol}"
            )
        old_line = lines[hours_indices[0]]
        old_content = content(old_line)
        indent = old_content[: len(old_content) - len(old_content.lstrip())]
        line_ending = old_line[len(old_content) :]
        if not line_ending:
            line_ending = "\r\n" if "\r\n" in "".join(lines) else "\n"
        value = ", ".join(json.dumps(hour) for hour in hours)
        hours_line = f"{indent}hours_utc = [{value}]{line_ending}"
        season_line = (
            f"{indent}hours_utc_season = {json.dumps(season)}{line_ending}"
        )
        symbol_changes: list[str] = []
        if content(lines[hours_indices[0]]) != content(hours_line):
            lines[hours_indices[0]] = hours_line
            symbol_changes.append(content(hours_line))
        if season_indices:
            if content(lines[season_indices[0]]) != content(season_line):
                lines[season_indices[0]] = season_line
                symbol_changes.append(content(season_line))
        else:
            lines.insert(hours_indices[0] + 1, season_line)
            symbol_changes.append(content(season_line))
        if symbol_changes:
            changed_lines[symbol] = tuple(symbol_changes)

    if not changed_lines:
        return {}

    updated = "".join(lines)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as settings_file:
            settings_file.write(updated)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return changed_lines


def _validate_schedule(hours: tuple[str, ...]) -> None:
    if not hours:
        raise ValueError("broker returned no sessions")
    sessions = parse_sessions(hours)
    failures: list[str] = []
    if not any(
        session.weekday == 4
        or (
            session.weekday + int(session.full_day or session.closes_utc < session.opens_utc)
        )
        % 7
        == 4
        for session in sessions
    ):
        failures.append("schedule has no Friday session")
    if session_windows_overlap(sessions):
        failures.append("schedule contains overlapping windows")
    if failures:
        raise ValueError("; ".join(failures))


def _valid_tick(tick: object | None, now: datetime) -> bool:
    return _tick_invalid_reason(tick, now) is None


def _tick_invalid_reason(tick: object | None, now: datetime) -> str | None:
    if tick is None:
        return "tick unavailable"
    time_utc = getattr(tick, "time_utc", None)
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    if not isinstance(time_utc, datetime):
        return "tick timestamp is missing or not a datetime"
    if time_utc.tzinfo is None or time_utc.utcoffset() != timedelta(0):
        return "tick timestamp is not timezone-aware UTC"
    if (
        not isinstance(bid, (int, float))
        or isinstance(bid, bool)
        or not math.isfinite(bid)
        or not isinstance(ask, (int, float))
        or isinstance(ask, bool)
        or not math.isfinite(ask)
    ):
        return "bid or ask is missing or non-finite"
    if bid <= 0:
        return f"bid must be positive (bid={bid})"
    if ask < bid:
        return f"ask is below bid (bid={bid}, ask={ask})"
    age = now - time_utc.astimezone(UTC)
    if age < -MAX_TICK_FUTURE_SKEW:
        return f"tick timestamp is {-age.total_seconds():.1f} seconds in the future"
    if age > MAX_TICK_AGE:
        return f"tick is stale by {age.total_seconds():.1f} seconds"
    return None


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("now_utc must be timezone-aware UTC")
    return value.astimezone(UTC)


def _report(
    symbol: str,
    enabled: bool,
    history_bars: int,
    spread_median_points: float | None,
    spread_p95_points: float | None,
    current_spread_points: float | None,
    spread_limit_points: float,
    suggested_hours_utc: tuple[str, ...],
    suggested_hours_utc_season: ScheduleSeason | None,
    issues: tuple[str, ...],
    weekday_candle_times: tuple[WeekdayCandleTimes, ...] = (),
    last_closed_candle_age_hours: float | None = None,
    tick_freshness: TickFreshnessDiagnostic | None = None,
    data_gaps: tuple[datetime, ...] = (),
    live_spread_median_points: float | None = None,
    live_spread_max_points: float | None = None,
    live_spread_sample_count: int = 0,
) -> SymbolValidationReport:
    return SymbolValidationReport(
        symbol=symbol,
        enabled=enabled,
        passed=not issues,
        history_bars=history_bars,
        spread_median_points=spread_median_points,
        spread_p95_points=spread_p95_points,
        current_spread_points=current_spread_points,
        spread_limit_points=spread_limit_points,
        suggested_hours_utc=suggested_hours_utc,
        suggested_hours_utc_season=suggested_hours_utc_season,
        issues=issues,
        weekday_candle_times=weekday_candle_times,
        last_closed_candle_age_hours=last_closed_candle_age_hours,
        tick_freshness=tick_freshness,
        data_gaps=data_gaps,
        live_spread_median_points=live_spread_median_points,
        live_spread_max_points=live_spread_max_points,
        live_spread_sample_count=live_spread_sample_count,
    )
