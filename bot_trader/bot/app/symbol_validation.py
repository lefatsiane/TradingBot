from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.adapters.broker import Broker
from bot.app.config import (
    ScheduleSeason,
    SymbolSettings,
    parse_sessions,
    schedule_season_for_utc,
    session_windows_overlap,
)
from bot.app.data import DataQualityError, validate_candles
from bot.core.indicators import atr
from bot.core.sizing import size_trade
from bot.core.types import Candle, Proposal, Side

MIN_HISTORY_DAYS = 183
MAX_TICK_AGE = timedelta(minutes=10)
MAX_HISTORY_END_AGE = timedelta(days=5)
ATR_PERIOD = 14
_H1 = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class SymbolValidationReport:
    symbol: str
    enabled: bool
    passed: bool
    history_bars: int
    max_observed_spread_points: float | None
    suggested_hours_utc: tuple[str, ...]
    suggested_hours_utc_season: ScheduleSeason | None
    issues: tuple[str, ...]


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
) -> SymbolValidationReport:
    now = _require_utc(now_utc)
    if equity <= 0 or risk_per_trade_pct <= 0 or max_stop_atr <= 0:
        raise ValueError("Equity, risk percentage, and maximum stop ATR must be positive")

    issues: list[str] = []
    try:
        spec = broker.symbol_info(symbol)
    except (OSError, RuntimeError, ValueError) as error:
        return _report(
            symbol, settings.enabled, 0, None, (), None, (f"symbol spec: {error}",)
        )

    try:
        tick = broker.tick(symbol)
    except (OSError, RuntimeError, ValueError) as error:
        tick = None
        issues.append(f"tick read failed: {error}")
    tick_valid = _valid_tick(tick, now)
    if not tick_valid and not any(issue.startswith("tick read failed:") for issue in issues):
        issues.append("no fresh, valid tick")

    cutoff = now - timedelta(days=MIN_HISTORY_DAYS)
    history_bars = 0
    recent: tuple[Candle, ...] = ()
    try:
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
                issues.append(f"H1 history quality: {error}")
            if recent[0].time > cutoff + _H1:
                issues.append("H1 history does not cover the full six-month validation window")
            if now - (recent[-1].time + _H1) > MAX_HISTORY_END_AGE:
                issues.append("H1 history is stale")

    current_spread_points: float | None = None
    if tick_valid and tick is not None:
        current_spread_points = (tick.ask - tick.bid) / spec.point
        if current_spread_points > settings.max_spread_points:
            issues.append(
                f"current spread {current_spread_points:.2f} points exceeds "
                f"{settings.max_spread_points:.2f}"
            )

    observed_spreads = [
        candle.spread / spec.point
        for candle in recent
        if candle.spread > 0
    ]
    if current_spread_points is not None:
        observed_spreads.append(current_spread_points)
    max_observed_spread = max(observed_spreads) if observed_spreads else None
    if recent and not any(candle.spread > 0 for candle in recent):
        issues.append("H1 history does not contain usable spread observations")
    if (
        max_observed_spread is not None
        and max_observed_spread > settings.max_spread_points
    ):
        issues.append(
            f"observed spread {max_observed_spread:.2f} points exceeds "
            f"{settings.max_spread_points:.2f}"
        )

    suggested_hours: tuple[str, ...] = ()
    suggested_season: ScheduleSeason | None = None
    try:
        suggested_hours = broker.symbol_sessions_utc(symbol)
        _validate_schedule(suggested_hours)
        suggested_season = schedule_season_for_utc(now)
    except (OSError, RuntimeError, ValueError) as error:
        issues.append(f"UTC sessions: {error}")

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
        max_observed_spread,
        suggested_hours,
        suggested_season,
        tuple(issues),
    )


def write_hours_utc(
    settings_path: str | Path,
    schedules: dict[str, tuple[str, ...]],
    season: ScheduleSeason,
) -> None:
    path = Path(settings_path)
    if not schedules:
        return
    with path.open("r", encoding="utf-8", newline="") as settings_file:
        lines = settings_file.read().splitlines(keepends=True)

    def content(line: str) -> str:
        return line.rstrip("\r\n")

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
        lines[hours_indices[0]] = f"{indent}hours_utc = [{value}]{line_ending}"
        season_line = (
            f"{indent}hours_utc_season = {json.dumps(season)}{line_ending}"
        )
        if season_indices:
            lines[season_indices[0]] = season_line
        else:
            lines.insert(hours_indices[0] + 1, season_line)

    updated = "".join(lines)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as settings_file:
            settings_file.write(updated)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
    if tick is None:
        return False
    time_utc = getattr(tick, "time_utc", None)
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    if (
        not isinstance(time_utc, datetime)
        or time_utc.tzinfo is None
        or time_utc.utcoffset() != timedelta(0)
        or not isinstance(bid, (int, float))
        or not isinstance(ask, (int, float))
        or bid <= 0
        or ask < bid
    ):
        return False
    age = now - time_utc.astimezone(UTC)
    return timedelta(0) <= age <= MAX_TICK_AGE


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("now_utc must be timezone-aware UTC")
    return value.astimezone(UTC)


def _report(
    symbol: str,
    enabled: bool,
    history_bars: int,
    max_observed_spread_points: float | None,
    suggested_hours_utc: tuple[str, ...],
    suggested_hours_utc_season: ScheduleSeason | None,
    issues: tuple[str, ...],
) -> SymbolValidationReport:
    return SymbolValidationReport(
        symbol=symbol,
        enabled=enabled,
        passed=not issues,
        history_bars=history_bars,
        max_observed_spread_points=max_observed_spread_points,
        suggested_hours_utc=suggested_hours_utc,
        suggested_hours_utc_season=suggested_hours_utc_season,
        issues=issues,
    )
