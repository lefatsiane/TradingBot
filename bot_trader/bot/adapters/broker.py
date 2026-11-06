from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from bot.core.types import Candle, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    is_demo: bool
    trade_allowed: bool
    margin_mode: str
    leverage: int = 100

    def __post_init__(self) -> None:
        if self.leverage <= 0:
            raise ValueError("Account leverage must be positive")


@dataclass(frozen=True, slots=True)
class BrokerTerminalInfo:
    connected: bool
    trade_allowed: bool
    name: str
    company: str


@dataclass(frozen=True, slots=True)
class Tick:
    time_utc: datetime
    bid: float
    ask: float


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticket: str
    symbol: str
    side: Side
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    magic_number: int
    comment: str
    opened_at_utc: datetime
    entry_commission: float = 0.0


@dataclass(frozen=True, slots=True)
class BrokerDeal:
    ticket: str
    position_ticket: str
    symbol: str
    side: Side
    lots: float
    price: float
    time_utc: datetime
    profit: float
    commission: float
    magic_number: int
    comment: str
    is_entry: bool | None = None


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    lots: float
    stop_loss: float
    take_profit: float
    magic_number: int
    intent_id: str


@dataclass(frozen=True, slots=True)
class BrokerResult:
    code: str
    ticket: str | None = None
    price: float | None = None
    lots: float | None = None
    message: str = ""
    commission: float | None = None


class Broker(Protocol):
    def connect(self) -> bool: ...

    def account(self) -> BrokerAccount: ...

    def terminal_info(self) -> BrokerTerminalInfo: ...

    def symbol_info(self, symbol: str) -> SymbolSpec: ...

    def symbol_sessions_utc(self, symbol: str) -> tuple[str, ...]: ...

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]: ...

    def tick(self, symbol: str) -> Tick | None: ...

    def server_utc_offset_hours(self) -> int: ...

    def calculate_margin(self, request: OrderRequest) -> float | None: ...

    def positions(self, magic_number: int) -> tuple[BrokerPosition, ...]: ...

    def history_deals(
        self, since_utc: datetime, magic_number: int
    ) -> tuple[BrokerDeal, ...]: ...

    def check(self, request: OrderRequest) -> BrokerResult: ...

    def send_market(self, request: OrderRequest) -> BrokerResult | None: ...

    def modify_sl_tp(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult: ...

    def close(self, ticket: str) -> BrokerResult: ...
