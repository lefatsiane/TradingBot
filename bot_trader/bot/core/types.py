from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class ReasonCode(StrEnum):
    STOP_NEW = "STOP_NEW"
    DAILY_HALT = "DAILY_HALT"
    MONTHLY_HALT = "MONTHLY_HALT"
    ERROR_HALT = "ERROR_HALT"
    WEEKEND_WINDOW = "WEEKEND_WINDOW"
    DATA_ERROR = "DATA_ERROR"
    SYMBOL_DISABLED = "SYMBOL_DISABLED"
    OUTSIDE_HOURS = "OUTSIDE_HOURS"
    NEWS_WINDOW = "NEWS_WINDOW"
    NEWS_CALENDAR_STALE = "NEWS_CALENDAR_STALE"
    ROLLOVER = "ROLLOVER"
    SPREAD_WIDE = "SPREAD_WIDE"
    SKIP_VOLATILITY = "SKIP_VOLATILITY"
    COOLDOWN = "COOLDOWN"
    WARMUP = "WARMUP"
    NO_TREND = "NO_TREND"
    NO_PULLBACK = "NO_PULLBACK"
    NO_TRIGGER = "NO_TRIGGER"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    NO_QUIET_MARKET = "NO_QUIET_MARKET"
    NO_STRETCH = "NO_STRETCH"
    REWARD_TOO_SMALL = "REWARD_TOO_SMALL"
    BELOW_MIN_LOT = "BELOW_MIN_LOT"
    COST_TOO_HIGH = "COST_TOO_HIGH"
    NO_MARGIN = "NO_MARGIN"
    CHECK_FAILED = "CHECK_FAILED"
    SEND_FAILED = "SEND_FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    TIME_STOP = "TIME_STOP"
    DAILY_HEADROOM = "DAILY_HEADROOM"
    SLOT_TAKEN = "SLOT_TAKEN"
    MAX_TRADES = "MAX_TRADES"
    CURRENCY_EXPOSURE = "CURRENCY_EXPOSURE"
    ENTRY_ALLOWED = "ENTRY_ALLOWED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TIME_STOP = "TIME_STOP"
    WEEKEND_FLATTEN = "WEEKEND_FLATTEN"
    MONTHLY_CAP = "MONTHLY_CAP"
    CLOSE_ALL = "CLOSE_ALL"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0
    tick_volume: int = 0

    def __post_init__(self) -> None:
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("Candle time must be timezone-aware")
        if self.time.utcoffset() != timedelta(0):
            raise ValueError("Candle time must be in UTC")
        if not all(isfinite(value) for value in (self.open, self.high, self.low, self.close)):
            raise ValueError("Candle prices must be finite")
        if not all(value > 0 for value in (self.open, self.high, self.low, self.close)):
            raise ValueError("Candle prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Candle high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Candle low is inconsistent")
        if not isfinite(self.spread) or self.spread < 0:
            raise ValueError("Candle spread must be finite and non-negative")
        if self.tick_volume < 0:
            raise ValueError("Candle tick volume cannot be negative")


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    name: str
    base_currency: str
    quote_currency: str
    point: float
    tick_size: float
    tick_value_loss: float
    volume_min: float
    volume_step: float
    volume_max: float
    digits: int = 0
    contract_size: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or not self.base_currency or not self.quote_currency:
            raise ValueError("Symbol and currency names are required")
        if any(
            value <= 0
            for value in (
                self.point,
                self.tick_size,
                self.tick_value_loss,
                self.volume_min,
                self.volume_step,
                self.volume_max,
                self.contract_size,
            )
        ):
            raise ValueError("Symbol price and volume specifications must be positive")
        if self.digits < 0:
            raise ValueError("Symbol digits cannot be negative")
        if self.volume_min > self.volume_max:
            raise ValueError("Minimum volume cannot exceed maximum volume")


@dataclass(frozen=True, slots=True)
class Proposal:
    side: Side
    entry: float
    stop_loss: float
    take_profit: float
    reason: str
    indicators: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if min(self.entry, self.stop_loss, self.take_profit) <= 0:
            raise ValueError("Proposal prices must be positive")
        if self.side is Side.LONG and not self.stop_loss < self.entry < self.take_profit:
            raise ValueError("Long proposal must have stop < entry < target")
        if self.side is Side.SHORT and not self.take_profit < self.entry < self.stop_loss:
            raise ValueError("Short proposal must have target < entry < stop")


@dataclass(frozen=True, slots=True)
class StrategyResult:
    proposal: Proposal | None
    reason: ReasonCode | None
    indicators: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class OpenRisk:
    symbol: str
    side: Side
    risk_pct: float

    def __post_init__(self) -> None:
        if not isfinite(self.risk_pct) or self.risk_pct < 0:
            raise ValueError("Risk percentage must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Position:
    ticket: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: datetime
    magic_number: int

    def __post_init__(self) -> None:
        if not self.ticket or not self.symbol:
            raise ValueError("Position ticket and symbol are required")
        if self.volume <= 0 or self.entry_price <= 0:
            raise ValueError("Position volume and entry price must be positive")
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("Position open time must be timezone-aware")
        if self.magic_number < 0:
            raise ValueError("Position magic number cannot be negative")
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("Position stop-loss must be positive")
        if self.take_profit is not None and self.take_profit <= 0:
            raise ValueError("Position take-profit must be positive")


@dataclass(frozen=True, slots=True)
class Decision:
    symbol: str
    time: datetime
    reason: ReasonCode
    strategy: str | None = None
    proposal: Proposal | None = None
    indicators: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Decision symbol is required")
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("Decision time must be timezone-aware")
