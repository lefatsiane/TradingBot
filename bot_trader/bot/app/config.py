from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bot.adapters.broker import BrokerAccount
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams

if TYPE_CHECKING:
    from bot.adapters.sim_broker import SimCosts


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountSettings(StrictModel):
    expected_login: int = Field(ge=0)
    expected_server: str = Field(min_length=1)
    mode: Literal["demo"] = "demo"
    allow_live: Literal[False] = False
    magic_number: int = Field(gt=0, le=2_147_483_647)


class RiskSettings(StrictModel):
    risk_per_trade_pct: float = Field(gt=0, le=0.5)
    daily_loss_pct: float = Field(gt=0, le=1.0)
    monthly_loss_pct: float = Field(gt=0, le=3.0)
    max_net_currency_exposure_pct: float = Field(gt=0, le=1.0)
    max_open_trades: int = Field(ge=1, le=3)
    cost_gate_pct_of_risk: float = Field(gt=0, le=15)
    max_margin_used_pct: float = Field(gt=0, le=30)
    cooldown_bars_after_stop: int = Field(ge=0, le=6)


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


class SymbolSettings(StrictModel):
    enabled: bool = True
    commission_per_lot_side: float = Field(ge=0)
    slippage_points: float = Field(ge=0)
    max_spread_points: float = Field(gt=0)
    hours_utc: tuple[str, ...] = ()


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
    min_candles: int = 600
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
            "min_candles": 600,
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
    canonical = json.dumps(
        settings.model_dump(mode="json"),
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
    mt5_password: str | None
    telegram_token: str | None
    healthcheck_url: str | None


def load_runtime_secrets(environ: Mapping[str, str] | None = None) -> RuntimeSecrets:
    """Read optional credentials from environment variables, never from TOML."""
    variables = os.environ if environ is None else environ
    return RuntimeSecrets(
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
