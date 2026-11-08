from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bot.adapters.broker import BrokerAccount
from bot.core.gates import SessionWindow
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from bot.core.timeutil import expected_new_york_offset

if TYPE_CHECKING:
    from bot.adapters.sim_broker import SimCosts


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ALLOW_RISK_LIMIT_INCREASES = False
_APPROVED_RISK_LIMITS = {
    "risk_per_trade_pct": 0.5,
    "daily_loss_pct": 1.0,
    "monthly_loss_pct": 3.0,
    "max_net_currency_exposure_pct": 1.0,
    "max_open_trades": 3,
    "cost_gate_pct_of_risk": 15.0,
    "max_margin_used_pct": 30.0,
    "cooldown_bars_after_stop": 6,
}


class AccountSettings(StrictModel):
    # MT5_LOGIN is the runtime source; this TOML compatibility field is ignored on load.
    expected_login: int = Field(ge=0)
    expected_server: str = Field(min_length=1)
    mode: Literal["demo"] = "demo"
    allow_live: Literal[False] = False
    magic_number: int = Field(gt=0, le=2_147_483_647)


class RiskSettings(StrictModel):
    risk_per_trade_pct: float = Field(gt=0, le=100)
    daily_loss_pct: float = Field(gt=0, le=100)
    monthly_loss_pct: float = Field(gt=0, le=100)
    max_net_currency_exposure_pct: float = Field(gt=0, le=100)
    max_open_trades: int = Field(ge=1, le=100)
    cost_gate_pct_of_risk: float = Field(gt=0, le=100)
    max_margin_used_pct: float = Field(gt=0, le=100)
    cooldown_bars_after_stop: int = Field(ge=0, le=1_000)

    @model_validator(mode="after")
    def enforce_approved_limits(self) -> RiskSettings:
        if ALLOW_RISK_LIMIT_INCREASES:
            return self
        for field_name, approved_limit in _APPROVED_RISK_LIMITS.items():
            if getattr(self, field_name) > approved_limit:
                raise ValueError(
                    f"{field_name} cannot exceed the approved limit {approved_limit}"
                )
        return self


class TimingSettings(StrictModel):
    fast_tick_s: int = Field(ge=1, le=2)
    candle_poll_s: int = Field(ge=1, le=5)
    no_new_entries_before_weekend_close_min: int = Field(ge=120)
    flatten_before_weekend_close_min: int = Field(ge=30)
    rollover_block_before_min: int = Field(ge=10)
    rollover_block_after_min: int = Field(ge=30)


class NewsSettings(StrictModel):
    calendar_file: str = Field(min_length=1)
    min_days_covered: int = Field(ge=7)
    block_before_min: int = Field(ge=30)
    block_after_min: int = Field(ge=30)


class GateSettings(StrictModel):
    max_candle_range_atr: float = Field(gt=0, le=3.0)


class ExecutionSettings(StrictModel):
    deviation_points: int = Field(ge=0)
    max_send_retries: int = Field(ge=0, le=1)
    unknown_outcome_wait_s: int = Field(ge=1)


class ControlSettings(StrictModel):
    telegram_chat_id: int
    command_max_age_s: int = Field(ge=60)


_DAY_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_TIME_UTC = r"(?:[01]\d|2[0-3]):[0-5]\d"
_HOURS_UTC_RE = re.compile(
    rf"^(mon|tue|wed|thu|fri|sat|sun)\s+({_TIME_UTC})-({_TIME_UTC}|24:00)$",
    re.IGNORECASE,
)
ScheduleSeason = Literal["US winter", "US summer"]


def parse_sessions(hours: tuple[str, ...]) -> tuple[SessionWindow, ...]:
    sessions: list[SessionWindow] = []
    for raw in hours:
        match = _HOURS_UTC_RE.fullmatch(raw.strip())
        if match is None:
            raise ValueError(f"Invalid hours_utc session: {raw!r}; expected 'mon 00:00-21:55'")
        weekday_name, start_text, end_text = match.groups()
        start = time.fromisoformat(start_text)
        end_of_day = end_text == "24:00"
        end = time(0) if end_of_day else time.fromisoformat(end_text)
        if start >= end and not end_of_day:
            raise ValueError(f"Invalid session window {raw!r}; end time must be after start time")
        weekday = _DAY_INDEX[weekday_name.lower()]
        sessions.append(
            SessionWindow(
                weekday=weekday,
                opens_utc=start,
                closes_utc=end,
                full_day=end_of_day and start == time(0),
            )
        )
    return tuple(sessions)


def session_windows_overlap(sessions: Iterable[SessionWindow]) -> bool:
    week_minutes = 7 * 24 * 60
    intervals: list[tuple[int, int]] = []
    for session in sessions:
        start = (
            session.weekday * 24 * 60
            + session.opens_utc.hour * 60
            + session.opens_utc.minute
        )
        end_minute = session.closes_utc.hour * 60 + session.closes_utc.minute
        duration = (
            24 * 60
            if session.full_day
            else end_minute
            - (session.opens_utc.hour * 60 + session.opens_utc.minute)
        )
        if duration < 0:
            duration += 24 * 60
        end = start + duration
        if end <= week_minutes:
            intervals.append((start, end))
        else:
            intervals.extend(((start, week_minutes), (0, end - week_minutes)))
    intervals.sort()
    return any(
        current[0] < previous[1]
        for previous, current in pairwise(intervals)
    )


def schedule_season_for_utc(utc_time: datetime) -> ScheduleSeason:
    return (
        "US summer"
        if expected_new_york_offset(utc_time) == 3
        else "US winter"
    )


def sessions_for_utc(
    hours: tuple[str, ...],
    reviewed_season: ScheduleSeason | None,
    at_utc: datetime,
) -> tuple[SessionWindow, ...]:
    sessions = parse_sessions(hours)
    if reviewed_season not in ("US winter", "US summer"):
        raise ValueError("A reviewed US season is required to resolve the UTC schedule")
    reviewed_offset = 3 if reviewed_season == "US summer" else 2
    active_offset = expected_new_york_offset(at_utc)
    shift_minutes = (reviewed_offset - active_offset) * 60
    if shift_minutes == 0:
        return sessions

    shifted: list[SessionWindow] = []
    for session in sessions:
        start_minute = session.opens_utc.hour * 60 + session.opens_utc.minute
        end_minute = session.closes_utc.hour * 60 + session.closes_utc.minute
        duration = (
            24 * 60
            if session.full_day
            else end_minute - start_minute
        )
        if duration < 0:
            duration += 24 * 60
        week_start = (
            session.weekday * 24 * 60 + start_minute + shift_minutes
        ) % (7 * 24 * 60)
        weekday, opens = divmod(week_start, 24 * 60)
        closes = (opens + duration) % (24 * 60)
        shifted.append(
            SessionWindow(
                weekday,
                time(opens // 60, opens % 60),
                time(closes // 60, closes % 60),
                full_day=duration == 24 * 60,
            )
        )
    return tuple(sorted(shifted, key=lambda session: (session.weekday, session.opens_utc)))


def schedule_validation_failures(
    settings: Settings,
    *,
    now_utc: datetime | None = None,
) -> tuple[str, ...]:
    failures: list[str] = []
    current_utc = datetime.now(tz=UTC) if now_utc is None else now_utc
    for symbol, symbol_settings in settings.symbols.items():
        if not symbol_settings.enabled:
            continue
        if not symbol_settings.hours_utc:
            failures.append(f"{symbol}: hours_utc is empty")
            continue
        if symbol_settings.hours_utc_season is None:
            failures.append(f"{symbol}: hours_utc_season is missing")
        try:
            sessions = (
                parse_sessions(symbol_settings.hours_utc)
                if symbol_settings.hours_utc_season is None
                else sessions_for_utc(
                    symbol_settings.hours_utc,
                    symbol_settings.hours_utc_season,
                    current_utc,
                )
            )
        except ValueError as error:
            failures.append(f"{symbol}: {error}")
            continue
        if not any(
            session.weekday == 4
            or (
                session.weekday
                + int(
                    session.full_day
                    or session.closes_utc < session.opens_utc
                )
            )
            % 7
            == 4
            for session in sessions
        ):
            failures.append(f"{symbol}: hours_utc has no Friday session")
        if session_windows_overlap(sessions):
            failures.append(f"{symbol}: overlapping UTC sessions")
    return tuple(failures)


class SymbolSettings(StrictModel):
    enabled: bool = True
    commission_per_lot_side: float = Field(ge=0)
    slippage_points: float = Field(ge=0)
    max_spread_points: float = Field(gt=0)
    hours_utc: tuple[str, ...] = ()
    hours_utc_season: ScheduleSeason | None = None

    @model_validator(mode="after")
    def validate_hours_utc(self) -> SymbolSettings:
        parse_sessions(self.hours_utc)
        if self.hours_utc and self.hours_utc_season is None:
            raise ValueError("hours_utc_season is required for a reviewed schedule")
        return self


class StrategyASettings(StrictModel):
    timeframes: tuple[Literal["H1", "H4"], ...] = ("H1", "H4")
    ema_fast: int = 20
    ema_trend: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    slope_lookback: int = 20
    pullback_window: int = 5
    stop_lookback: int = 5
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    take_profit_r: float = 2.0
    min_candles: int = 300
    window_candles: int = 1_000
    cooldown_bars_after_stop: int = 6

    @model_validator(mode="after")
    def require_approved_values(self) -> StrategyASettings:
        expected = {
            "timeframes": ("H1", "H4"),
            "ema_fast": 20,
            "ema_trend": 50,
            "ema_slow": 200,
            "atr_period": 14,
            "slope_lookback": 20,
            "pullback_window": 5,
            "stop_lookback": 5,
            "stop_buffer_atr": 0.25,
            "min_stop_atr": 1.0,
            "max_stop_atr": 3.0,
            "take_profit_r": 2.0,
            "min_candles": 300,
            "window_candles": 1_000,
            "cooldown_bars_after_stop": 6,
        }
        if self.model_dump() != expected:
            raise ValueError("Strategy A parameters are fixed to the approved design values")
        return self

    def to_params(self) -> StrategyAParams:
        return StrategyAParams(
            ema_fast=self.ema_fast,
            ema_mid=self.ema_trend,
            ema_slow=self.ema_slow,
            atr_period=self.atr_period,
            slope_lookback=self.slope_lookback,
            pullback_window=self.pullback_window,
            stop_lookback=self.stop_lookback,
            stop_buffer_atr=self.stop_buffer_atr,
            min_stop_atr=self.min_stop_atr,
            max_stop_atr=self.max_stop_atr,
            take_profit_r=self.take_profit_r,
            min_candles=self.min_candles,
            window_candles=self.window_candles,
            cooldown_bars_after_stop=self.cooldown_bars_after_stop,
        )


class StrategyBSettings(StrictModel):
    bollinger_period: int = 20
    bollinger_deviations: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    adx_period: int = 14
    adx_quiet_max: float = 20.0
    atr_period: int = 14
    stop_lookback: int = 3
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    min_reward_r: float = 1.0
    time_stop_candles: int = 24
    cooldown_bars_after_stop: int = 6

    @model_validator(mode="after")
    def require_approved_values(self) -> StrategyBSettings:
        expected = {
            "bollinger_period": 20,
            "bollinger_deviations": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 30.0,
            "rsi_overbought": 70.0,
            "adx_period": 14,
            "adx_quiet_max": 20.0,
            "atr_period": 14,
            "stop_lookback": 3,
            "stop_buffer_atr": 0.25,
            "min_stop_atr": 1.0,
            "max_stop_atr": 3.0,
            "min_reward_r": 1.0,
            "time_stop_candles": 24,
            "cooldown_bars_after_stop": 6,
        }
        if self.model_dump() != expected:
            raise ValueError("Strategy B parameters are fixed to the approved design values")
        return self

    def to_params(self) -> StrategyBParams:
        return StrategyBParams(
            bollinger_period=self.bollinger_period,
            bollinger_deviations=self.bollinger_deviations,
            rsi_period=self.rsi_period,
            rsi_oversold=self.rsi_oversold,
            rsi_overbought=self.rsi_overbought,
            adx_period=self.adx_period,
            adx_quiet_max=self.adx_quiet_max,
            atr_period=self.atr_period,
            stop_lookback=self.stop_lookback,
            stop_buffer_atr=self.stop_buffer_atr,
            min_stop_atr=self.min_stop_atr,
            max_stop_atr=self.max_stop_atr,
            min_reward_r=self.min_reward_r,
            time_stop_candles=self.time_stop_candles,
            cooldown_bars_after_stop=self.cooldown_bars_after_stop,
        )


class Settings(StrictModel):
    schema_version: Literal[1]
    account: AccountSettings
    risk: RiskSettings
    timing: TimingSettings
    news: NewsSettings
    gates: GateSettings
    execution: ExecutionSettings
    control: ControlSettings
    symbols: dict[str, SymbolSettings] = Field(min_length=1)
    strategy_a: StrategyASettings
    strategy_b: StrategyBSettings

    @model_validator(mode="after")
    def require_expected_forex_set(self) -> Settings:
        allowed = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "XAUUSD"}
        unexpected = set(self.symbols) - allowed
        if unexpected:
            raise ValueError(f"Out-of-scope symbols in settings: {', '.join(sorted(unexpected))}")
        return self


def load_settings(path: str | Path) -> tuple[Settings, str]:
    settings_path = Path(path)
    with settings_path.open("rb") as settings_file:
        raw = tomllib.load(settings_file)
    settings = Settings.model_validate(raw)
    settings = settings.model_copy(
        update={
            "account": settings.account.model_copy(update={"expected_login": 0})
        }
    )
    canonical_settings = settings.model_dump(mode="json")
    canonical = json.dumps(
        canonical_settings,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return settings, hashlib.sha256(canonical).hexdigest()


def build_simulator_costs(
    settings: Settings, symbols: Iterable[str] | None = None
) -> dict[str, SimCosts]:
    """Translate configured per-symbol commission and slippage to simulator costs."""
    from bot.adapters.sim_broker import SimCosts

    selected = set(settings.symbols) if symbols is None else set(symbols)
    unknown = selected - settings.symbols.keys()
    if unknown:
        raise ValueError(f"Symbols have no configured costs: {', '.join(sorted(unknown))}")
    return {
        symbol: SimCosts(
            commission_per_lot_side=settings.symbols[symbol].commission_per_lot_side,
            slippage_points=settings.symbols[symbol].slippage_points,
        )
        for symbol in sorted(selected)
    }


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    mt5_login: int | None
    mt5_password: str | None
    telegram_token: str | None
    healthcheck_url: str | None


def load_runtime_secrets(environ: Mapping[str, str] | None = None) -> RuntimeSecrets:
    """Read optional credentials from environment variables, never from TOML."""
    variables = os.environ if environ is None else environ
    raw_login = variables.get("MT5_LOGIN")
    if raw_login:
        try:
            mt5_login = int(raw_login)
        except ValueError as error:
            raise ValueError("MT5_LOGIN must be a positive integer") from error
        if mt5_login <= 0:
            raise ValueError("MT5_LOGIN must be a positive integer")
    else:
        mt5_login = None
    return RuntimeSecrets(
        mt5_login=mt5_login,
        mt5_password=variables.get("MT5_PASSWORD") or None,
        telegram_token=variables.get("TELEGRAM_TOKEN") or None,
        healthcheck_url=variables.get("HEALTHCHECK_URL") or None,
    )


def startup_guard(
    settings: Settings,
    account: BrokerAccount,
    *,
    terminal_connected: bool,
    terminal_trade_allowed: bool,
) -> tuple[str, ...]:
    """Return all account and terminal mismatches that must block startup."""
    failures: list[str] = []
    if account.login != settings.account.expected_login:
        failures.append("account login does not match expected_login")
    if account.server != settings.account.expected_server:
        failures.append("account server does not match expected_server")
    expected_demo = settings.account.mode == "demo"
    if account.is_demo != expected_demo:
        failures.append("account type does not match configured mode")
    if not account.margin_mode:
        failures.append("account margin mode is unavailable")
    if not terminal_connected:
        failures.append("MetaTrader terminal is not connected")
    if not terminal_trade_allowed:
        failures.append("algorithmic trading is disabled in the terminal")
    if not account.trade_allowed:
        failures.append("trading is not allowed for the account")
    if not expected_demo and not settings.account.allow_live:
        failures.append("live trading is disabled by configuration")
    return tuple(failures)
