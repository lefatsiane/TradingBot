from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from bot.adapters.broker import (
    Broker,
    BrokerAccount,
    BrokerDeal,
    BrokerPosition,
    BrokerResult,
    BrokerTerminalInfo,
    OrderRequest,
    Tick,
)
from bot.core.types import Candle, SymbolSpec

T = TypeVar("T")


class BrokerGateway:
    """Serialize broker access and retry only idempotent reads after transport failures."""

    def __init__(
        self,
        broker: Broker,
        *,
        lock_timeout_s: float = 20.0,
        max_retries: int = 6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if lock_timeout_s <= 0 or max_retries < 0:
            raise ValueError("Gateway timeout must be positive and retries non-negative")
        self._broker = broker
        self._lock_timeout_s = lock_timeout_s
        self._max_retries = max_retries
        self._sleep = sleep
        self._lock = threading.Lock()
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _invoke(
        self,
        operation: Callable[..., T],
        *args: object,
        retryable: bool = False,
    ) -> T:
        retries = 0
        delay_s = 1.0
        method = getattr(operation, "__name__", "broker operation")
        while True:
            if not self._lock.acquire(timeout=self._lock_timeout_s):
                self._degraded = True
                raise TimeoutError(f"Timed out waiting for broker lock: {method}")
            try:
                result = operation(*args)
            except (ConnectionError, TimeoutError):
                self._degraded = True
                if not retryable or retries >= self._max_retries:
                    raise
            else:
                self._degraded = False
                return result
            finally:
                self._lock.release()

            self._sleep(delay_s)
            retries += 1
            delay_s = min(delay_s * 2.0, 30.0)

    def connect(self) -> bool:
        return self._invoke(self._broker.connect, retryable=True)

    def account(self) -> BrokerAccount:
        return self._invoke(self._broker.account, retryable=True)

    def terminal_info(self) -> BrokerTerminalInfo:
        return self._invoke(self._broker.terminal_info, retryable=True)

    def symbol_info(self, symbol: str) -> SymbolSpec:
        return self._invoke(self._broker.symbol_info, symbol, retryable=True)

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]:
        return self._invoke(self._broker.candles, symbol, timeframe, retryable=True)

    def tick(self, symbol: str) -> Tick | None:
        return self._invoke(self._broker.tick, symbol, retryable=True)

    def server_utc_offset_hours(self) -> int:
        return self._invoke(self._broker.server_utc_offset_hours, retryable=True)

    def calculate_margin(self, request: OrderRequest) -> float | None:
        return self._invoke(self._broker.calculate_margin, request, retryable=True)

    def positions(self, magic_number: int) -> tuple[BrokerPosition, ...]:
        return self._invoke(self._broker.positions, magic_number, retryable=True)

    def history_deals(
        self, since_utc: datetime, magic_number: int
    ) -> tuple[BrokerDeal, ...]:
        return self._invoke(
            self._broker.history_deals,
            since_utc,
            magic_number,
            retryable=True,
        )

    def check(self, request: OrderRequest) -> BrokerResult:
        return self._invoke(self._broker.check, request, retryable=True)

    def send_market(self, request: OrderRequest) -> BrokerResult | None:
        return self._invoke(self._broker.send_market, request)

    def modify_sl_tp(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult:
        return self._invoke(self._broker.modify_sl_tp, ticket, stop_loss, take_profit)

    def close(self, ticket: str) -> BrokerResult:
        return self._invoke(self._broker.close, ticket)
