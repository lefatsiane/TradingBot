from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import MetaTrader5 as mt5

from bot.adapters.broker import Tick
from bot.core.types import Candle, SymbolSpec


class _SymbolInfo(Protocol):
    currency_base: str
    currency_profit: str
    point: float
    trade_tick_size: float
    trade_tick_value_loss: float
    volume_min: float
    volume_step: float
    volume_max: float
    digits: int
    trade_contract_size: float


class _TickInfo(Protocol):
    time: int
    time_msc: int
    bid: float
    ask: float


class _Rate(Protocol):
    def __getitem__(self, field: str, /) -> object: ...


class _MT5API(Protocol):
    TIMEFRAME_H1: int
    TIMEFRAME_H4: int

    def initialize(self) -> bool: ...

    def shutdown(self) -> None: ...

    def last_error(self) -> object: ...

    def terminal_info(self) -> object | None: ...

    def symbol_info(self, symbol: str) -> _SymbolInfo | None: ...

    def symbol_info_tick(self, symbol: str) -> _TickInfo | None: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> Sequence[_Rate] | None: ...


class MT5ReadOnlyBroker:
    """Read-only MT5 history adapter; it has no order-send methods."""

    def __init__(
        self,
        *,
        api: _MT5API | None = None,
        history_start_utc: datetime | None = None,
        history_end_utc: datetime | None = None,
        server_offset_provider: Callable[[], object] | None = None,
    ) -> None:
        self._api = cast(_MT5API, mt5) if api is None else api
        self._history_start_utc = history_start_utc
        self._history_end_utc = history_end_utc
        self._server_offset_provider = server_offset_provider
        if history_start_utc is not None:
            _require_utc(history_start_utc, "history_start_utc")
        if history_end_utc is not None:
            _require_utc(history_end_utc, "history_end_utc")
        if (
            history_start_utc is not None
            and history_end_utc is not None
            and history_start_utc >= history_end_utc
        ):
            raise ValueError("History start must be earlier than history end")

    def connect(self) -> bool:
        if not self._api.initialize():
            raise ConnectionError(f"MetaTrader 5 initialize failed: {self._api.last_error()}")
        return True

    def shutdown(self) -> None:
        self._api.shutdown()

    def symbol_info(self, symbol: str) -> SymbolSpec:
        info = self._api.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MetaTrader 5 symbol_info failed for {symbol}: {self._api.last_error()}")
        if not info.currency_base or not info.currency_profit:
            raise ValueError(f"MT5 symbol {symbol} has no base/profit currencies")
        if info.trade_tick_value_loss <= 0:
            raise ValueError(f"MT5 symbol {symbol} has no positive loss tick value")
        return SymbolSpec(
            name=symbol,
            base_currency=info.currency_base,
            quote_currency=info.currency_profit,
            point=info.point,
            tick_size=info.trade_tick_size,
            tick_value_loss=info.trade_tick_value_loss,
            volume_min=info.volume_min,
            volume_step=info.volume_step,
            volume_max=info.volume_max,
            digits=info.digits,
            contract_size=info.trade_contract_size,
        )

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]:
        timeframe_constant = {
            "H1": self._api.TIMEFRAME_H1,
            "H4": self._api.TIMEFRAME_H4,
        }.get(timeframe)
        if timeframe_constant is None:
            raise ValueError(f"Unsupported MT5 history timeframe: {timeframe}")
        now = datetime.now(tz=UTC)
        end = self._history_end_utc or now
        start = self._history_start_utc or end - timedelta(days=365 * 6)
        rates_by_time: dict[int, _Rate] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=180), end)
            rates = self._api.copy_rates_range(
                symbol,
                timeframe_constant,
                cursor,
                chunk_end,
            )
            if rates is None:
                raise RuntimeError(
                    f"MetaTrader 5 history request failed for {symbol} {timeframe}: "
                    f"{self._api.last_error()}"
                )
            for rate in rates:
                rates_by_time.setdefault(int(rate["time"]), rate)
            cursor = chunk_end
        if not rates_by_time:
            raise RuntimeError(f"MetaTrader 5 returned no history for {symbol} {timeframe}")
        spec = self.symbol_info(symbol)
        candles = tuple(
            Candle(
                time=datetime.fromtimestamp(int(rate["time"]), tz=UTC),
                open=float(rate["open"]),
                high=float(rate["high"]),
                low=float(rate["low"]),
                close=float(rate["close"]),
                spread=float(rate["spread"]) * spec.point,
                tick_volume=int(rate["tick_volume"]),
            )
            for _, rate in sorted(rates_by_time.items())
        )
        return candles

    def tick(self, symbol: str) -> Tick | None:
        info = self._api.symbol_info_tick(symbol)
        if info is None:
            return None
        timestamp = info.time_msc / 1_000 if info.time_msc else info.time
        return Tick(datetime.fromtimestamp(timestamp, tz=UTC), info.bid, info.ask)

    def server_utc_offset_hours(self) -> int:
        if self._server_offset_provider is not None:
            offset = self._server_offset_provider()
        else:
            terminal = self._api.terminal_info()
            offset = getattr(terminal, "server_utc_offset_hours", None)
            if offset is None:
                raise RuntimeError(
                    "The standard MT5 Python API does not expose a server UTC offset. "
                    "Supply a broker-reported offset provider; do not infer it from UTC ticks."
                )
        if isinstance(offset, bool) or not isinstance(offset, int) or not -12 <= offset <= 14:
            raise ValueError("Broker server UTC offset must be an integer in -12..14")
        return offset


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
