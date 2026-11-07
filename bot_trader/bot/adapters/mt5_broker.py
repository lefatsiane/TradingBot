from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Protocol, cast

import MetaTrader5 as mt5

from bot.adapters.broker import (
    BrokerAccount,
    BrokerDeal,
    BrokerPosition,
    BrokerResult,
    BrokerTerminalInfo,
    OrderRequest,
    Tick,
)
from bot.core.timeutil import (
    broker_wall_time_to_utc,
    expected_new_york_offset,
    rounded_server_offset_from_tick,
)
from bot.core.types import Candle, Side, SymbolSpec

SERVER_OFFSET_REFERENCE_SYMBOL = "EURUSD"
SERVER_OFFSET_MAX_TICK_AGE_S = 60
SERVER_OFFSET_SAMPLE_COUNT = 3
SERVER_OFFSET_MAX_SAMPLE_ATTEMPTS = 30
SERVER_OFFSET_SAMPLE_INTERVAL_S = 0.1
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    filling_mode: int


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
    TRADE_ACTION_DEAL: int
    TRADE_ACTION_SLTP: int
    ORDER_TYPE_BUY: int
    ORDER_TYPE_SELL: int
    ORDER_TYPE_BUY_LIMIT: int
    ORDER_TYPE_SELL_LIMIT: int
    ORDER_TYPE_BUY_STOP: int
    ORDER_TYPE_SELL_STOP: int
    ORDER_FILLING_FOK: int
    ORDER_FILLING_IOC: int
    SYMBOL_FILLING_FOK: int
    SYMBOL_FILLING_IOC: int
    ORDER_TIME_GTC: int
    ACCOUNT_TRADE_MODE_DEMO: int
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING: int
    ACCOUNT_MARGIN_MODE_EXCHANGE: int
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: int
    TRADE_RETCODE_DONE: int
    TRADE_RETCODE_INVALID_STOPS: int
    TRADE_RETCODE_INVALID_FILL: int
    TRADE_RETCODE_REQUOTE: int
    TRADE_RETCODE_REJECTED: int
    TRADE_RETCODE_NO_MONEY: int
    TRADE_RETCODE_MARKET_CLOSED: int
    TRADE_RETCODE_TIMEOUT: int
    TRADE_RETCODE_PRICE_OFF: int
    TRADE_RETCODE_CONNECTION: int
    TRADE_RETCODE_REJECT: int
    TRADE_RETCODE_UNKNOWN: int

    def initialize(self) -> bool: ...

    def shutdown(self) -> None: ...

    def login(self, login: int, password: str, server: str) -> bool: ...

    def last_error(self) -> object: ...

    def terminal_info(self) -> object | None: ...

    def account_info(self) -> object | None: ...

    def symbol_info(self, symbol: str) -> _SymbolInfo | None: ...

    def symbol_select(self, symbol: str, enable: bool) -> bool: ...

    def symbol_info_session_trade(
        self,
        symbol: str,
        day_of_week: int,
        session_index: int,
    ) -> object | None: ...

    def symbol_info_tick(self, symbol: str) -> _TickInfo | None: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> Sequence[_Rate] | None: ...

    def positions_get(self, group: str = "", symbol: str = "") -> Sequence[object] | None: ...

    def history_deals_get(
        self,
        date_from: datetime,
        date_to: datetime | None = None,
        group: str = "",
        symbol: str = "",
    ) -> Sequence[object] | None: ...

    def order_calc_margin(
        self,
        action: int,
        symbol: str,
        volume: float,
        price: float,
    ) -> float | None: ...

    def order_check(self, request: dict[str, Any]) -> object | None: ...

    def order_send(self, request: dict[str, Any]) -> object | None: ...


class MT5ReadOnlyBroker:
    """Read-only MT5 history adapter; it has no order-send methods."""

    def __init__(
        self,
        *,
        api: _MT5API | None = None,
        history_start_utc: datetime | None = None,
        history_end_utc: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api = cast(_MT5API, mt5) if api is None else api
        self._history_start_utc = history_start_utc
        self._history_end_utc = history_end_utc
        self._clock = (lambda: datetime.now(tz=UTC)) if clock is None else clock
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
                time=broker_wall_time_to_utc(int(rate["time"])),
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
        server_epoch = info.time_msc / 1_000 if info.time_msc else info.time
        return Tick(broker_wall_time_to_utc(server_epoch), info.bid, info.ask)

    def server_utc_offset_hours(self) -> int:
        return expected_new_york_offset(_require_utc(self._clock(), "clock"))


class MT5Broker:
    """MetaTrader 5 order adapter for the broker protocol."""

    def __init__(
        self,
        *,
        api: _MT5API | None = None,
        server_clock_sanity_provider: Callable[[], object] | None = None,
        on_server_clock_mismatch: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        deviation_points: int = 30,
    ) -> None:
        self._api = cast(_MT5API, mt5) if api is None else api
        self._server_clock_sanity_provider = server_clock_sanity_provider
        self._on_server_clock_mismatch = on_server_clock_mismatch
        self._server_clock_checked = False
        self._clock = (lambda: datetime.now(tz=UTC)) if clock is None else clock
        self._deviation_points = deviation_points
        self._connected = False
        self._login: int | None = None
        self._server: str | None = None
        self._account_mode: str = "unknown"

    def connect(
        self,
        *,
        login: int | None = None,
        server: str | None = None,
        password: str | None = None,
        password_env_var: str = "MT5_PASSWORD",
    ) -> bool:
        if login is not None:
            self._login = login
        if server is not None:
            self._server = server
        if self._login is None:
            raise ValueError("MT5 login is required")
        if self._server is None:
            raise ValueError("MT5 server is required")
        if password is None:
            password = os.environ.get(password_env_var)
        if not password:
            raise RuntimeError(f"MT5 password is not set in environment variable {password_env_var}")
        if not self._api.initialize():
            raise ConnectionError(f"MetaTrader 5 initialize failed: {self._api.last_error()}")
        login_succeeded = False
        demo_account = False
        try:
            login_succeeded = self._api.login(self._login, password, self._server)
            if login_succeeded:
                demo_account = self._is_demo_account()
        finally:
            if not login_succeeded or not demo_account:
                self._api.shutdown()
        if not login_succeeded:
            raise ConnectionError(f"MetaTrader 5 login failed: {self._api.last_error()}")
        if not demo_account:
            raise PermissionError("MetaTrader 5 adapter only permits demo accounts")
        self._connected = True
        return True

    def shutdown(self) -> None:
        self._connected = False
        self._api.shutdown()

    def account(self) -> BrokerAccount:
        info = self._api.account_info()
        if info is None:
            raise RuntimeError(f"MetaTrader 5 account_info failed: {self._api.last_error()}")
        margin_mode = getattr(info, "margin_mode", None)
        self._account_mode = _margin_mode_name(margin_mode, self._api)
        return BrokerAccount(
            login=int(getattr(info, "login", self._login or 0)),
            server=str(getattr(info, "server", self._server or "")),
            currency=str(getattr(info, "currency", "USD")).upper(),
            balance=float(getattr(info, "balance", 0.0)),
            equity=float(getattr(info, "equity", getattr(info, "balance", 0.0))),
            is_demo=self._is_demo_account(info),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            margin_mode=self._account_mode,
            leverage=int(getattr(info, "leverage", 100)),
        )

    def terminal_info(self) -> BrokerTerminalInfo:
        info = self._api.terminal_info()
        if info is None:
            raise RuntimeError(f"MetaTrader 5 terminal_info failed: {self._api.last_error()}")
        return BrokerTerminalInfo(
            connected=bool(getattr(info, "connected", self._connected)),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            name=str(getattr(info, "name", "MetaTrader 5")),
            company=str(getattr(info, "company", "MetaQuotes")),
        )

    def symbol_info(self, symbol: str) -> SymbolSpec:
        if not self._api.symbol_select(symbol, True):
            raise RuntimeError(f"MetaTrader 5 symbol_select failed for {symbol}: {self._api.last_error()}")
        info = self._api.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MetaTrader 5 symbol_info failed for {symbol}: {self._api.last_error()}")
        return SymbolSpec(
            name=symbol,
            base_currency=str(getattr(info, "currency_base", "USD")),
            quote_currency=str(getattr(info, "currency_profit", "USD")),
            point=float(getattr(info, "point", 0.00001)),
            tick_size=float(getattr(info, "trade_tick_size", getattr(info, "point", 0.00001))),
            tick_value_loss=float(getattr(info, "trade_tick_value_loss", 1.0)),
            volume_min=float(getattr(info, "volume_min", 0.01)),
            volume_step=float(getattr(info, "volume_step", 0.01)),
            volume_max=float(getattr(info, "volume_max", 100.0)),
            digits=int(getattr(info, "digits", 5)),
            contract_size=float(getattr(info, "trade_contract_size", 100_000.0)),
        )

    def symbol_sessions_utc(self, symbol: str) -> tuple[str, ...]:
        self.symbol_info(symbol)
        offset_hours = self.server_utc_offset_hours()
        windows: list[tuple[int, int, int]] = []
        for weekday in range(7):
            session_index = 0
            while True:
                session = self._api.symbol_info_session_trade(
                    symbol,
                    weekday,
                    session_index,
                )
                if session is None:
                    break
                raw_start = getattr(session, "from", None)
                raw_end = getattr(session, "to", None)
                if (
                    isinstance(raw_start, bool)
                    or not isinstance(raw_start, int)
                    or isinstance(raw_end, bool)
                    or not isinstance(raw_end, int)
                    or raw_start < 0
                    or raw_start >= 24 * 60 * 60
                    or raw_end < 0
                    or raw_end > 24 * 60 * 60
                ):
                    raise ValueError(f"Invalid MT5 session times for {symbol}")
                if raw_end == raw_start:
                    session_index += 1
                    continue
                end_seconds = raw_end
                if end_seconds < raw_start:
                    end_seconds += 24 * 60 * 60
                if end_seconds - raw_start > 24 * 60 * 60:
                    raise ValueError(f"MT5 session exceeds 24 hours for {symbol}")
                start_week_seconds = (
                    weekday * 24 * 60 * 60
                    + raw_start
                    - offset_hours * 60 * 60
                )
                end_week_seconds = (
                    weekday * 24 * 60 * 60
                    + end_seconds
                    - offset_hours * 60 * 60
                )
                while start_week_seconds < end_week_seconds:
                    day_index = start_week_seconds // (24 * 60 * 60)
                    segment_end = min(
                        end_week_seconds,
                        (day_index + 1) * 24 * 60 * 60,
                    )
                    start_minute = (start_week_seconds + 59) // 60
                    end_minute = segment_end // 60
                    if end_minute > start_minute:
                        open_minute = start_minute % (24 * 60)
                        close_minute = end_minute - day_index * 24 * 60
                        windows.append(
                            (
                                day_index % 7,
                                open_minute,
                                min(close_minute, 24 * 60 - 1),
                            )
                        )
                    start_week_seconds = segment_end
                session_index += 1

        if not windows:
            raise RuntimeError(f"MT5 returned no trading sessions for {symbol}")
        windows.sort()
        return tuple(
            f"{_WEEKDAY_NAMES[weekday]} "
            f"{open_minute // 60:02}:{open_minute % 60:02}-"
            f"{close_minute // 60:02}:{close_minute % 60:02}"
            for weekday, open_minute, close_minute in windows
        )

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]:
        read_only = MT5ReadOnlyBroker(api=self._api)
        return read_only.candles(symbol, timeframe)

    def tick(self, symbol: str) -> Tick | None:
        info = self._api.symbol_info_tick(symbol)
        if info is None:
            return None
        server_epoch = (
            info.time_msc / 1_000
            if getattr(info, "time_msc", 0)
            else info.time
        )
        return Tick(
            broker_wall_time_to_utc(server_epoch),
            float(info.bid),
            float(info.ask),
        )

    def server_utc_offset_hours(self) -> int:
        now_utc = _require_utc(self._clock(), "clock")
        expected_offset = expected_new_york_offset(now_utc)
        if not self._server_clock_checked:
            mismatch = self._server_clock_sanity_mismatch(expected_offset)
            if mismatch is not None:
                if self._on_server_clock_mismatch is None:
                    raise RuntimeError(mismatch)
                self._on_server_clock_mismatch(mismatch)
            self._server_clock_checked = True
        return expected_offset

    def _server_clock_sanity_mismatch(self, expected_offset: int) -> str | None:
        try:
            observed_offset = (
                self._observed_server_offset_from_ticks(expected_offset)
                if self._server_clock_sanity_provider is None
                else self._observed_server_offset_from_provider()
            )
        except (OSError, OverflowError, RuntimeError, ValueError) as error:
            return f"Could not verify the MT5 server clock: {error}"
        if observed_offset != expected_offset:
            return (
                f"MT5 server clock reports UTC{observed_offset:+g}, but the "
                f"New York + 7h rule expects UTC{expected_offset:+d}"
            )
        return None

    def _observed_server_offset_from_provider(self) -> float:
        value = self._server_clock_sanity_provider()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError("server clock sanity value must be finite numeric hours")
        return float(value)

    def _observed_server_offset_from_ticks(self, expected_offset: int) -> float:
        observed_offsets: list[float] = []
        last_time_msc = 0
        attempts = 0
        while (
            len(observed_offsets) < SERVER_OFFSET_SAMPLE_COUNT
            and attempts < SERVER_OFFSET_MAX_SAMPLE_ATTEMPTS
        ):
            attempts += 1
            tick = self._api.symbol_info_tick(SERVER_OFFSET_REFERENCE_SYMBOL)
            if tick is not None:
                time_msc = getattr(tick, "time_msc", 0)
                if isinstance(time_msc, bool) or not isinstance(time_msc, int) or time_msc <= 0:
                    raise ValueError("EURUSD tick has no valid time_msc")
                if time_msc < last_time_msc:
                    raise ValueError("EURUSD tick time_msc moved backwards")
                if time_msc > last_time_msc:
                    now_utc = _require_utc(self._clock(), "clock")
                    observed_offset = rounded_server_offset_from_tick(
                        time_msc, now_utc
                    )
                    tick_utc = datetime.fromtimestamp(time_msc / 1_000, tz=UTC)
                    age = abs(
                        (
                            now_utc
                            - (tick_utc - timedelta(hours=observed_offset))
                        ).total_seconds()
                    )
                    if age > SERVER_OFFSET_MAX_TICK_AGE_S:
                        raise RuntimeError("EURUSD sanity tick is stale")
                    observed_offsets.append(observed_offset)
                    last_time_msc = time_msc
            if len(observed_offsets) < SERVER_OFFSET_SAMPLE_COUNT:
                time.sleep(SERVER_OFFSET_SAMPLE_INTERVAL_S)
        if len(observed_offsets) != SERVER_OFFSET_SAMPLE_COUNT:
            raise RuntimeError(
                f"EURUSD time_msc did not advance across "
                f"{SERVER_OFFSET_SAMPLE_COUNT} fresh ticks"
            )
        if any(value != observed_offsets[0] for value in observed_offsets[1:]):
            raise RuntimeError("EURUSD tick offsets were inconsistent")
        return observed_offsets[0]

    def calculate_margin(self, request: OrderRequest) -> float | None:
        price = self._execution_price_for(request)
        if price is None:
            return None
        result = self._api.order_calc_margin(
            self._api.TRADE_ACTION_DEAL,
            request.symbol,
            request.lots,
            price,
        )
        if result is None:
            return None
        return float(result)

    def positions(self, magic_number: int) -> tuple[BrokerPosition, ...]:
        raw = self._api.positions_get(group="", symbol="")
        if raw is None:
            raise RuntimeError(f"MetaTrader 5 positions_get failed: {self._api.last_error()}")
        if not raw:
            return ()
        self.server_utc_offset_hours()
        positions: list[BrokerPosition] = []
        for item in raw:
            item_magic = int(getattr(item, "magic", 0))
            if item_magic != magic_number:
                continue
            side_value = getattr(item, "type", 0)
            side = Side.LONG if side_value in (self._api.ORDER_TYPE_BUY, self._api.ORDER_TYPE_BUY_LIMIT, self._api.ORDER_TYPE_BUY_STOP) else Side.SHORT
            ticket = str(getattr(item, "ticket", item_magic))
            positions.append(
                BrokerPosition(
                    ticket=ticket,
                    symbol=str(getattr(item, "symbol", "")),
                    side=side,
                    lots=float(getattr(item, "volume", 0.0)),
                    entry=float(getattr(item, "price_open", 0.0)),
                    stop_loss=float(getattr(item, "sl", 0.0) or 0.0),
                    take_profit=float(getattr(item, "tp", 0.0) or 0.0),
                    magic_number=item_magic,
                    comment=str(getattr(item, "comment", "")),
                    opened_at_utc=broker_wall_time_to_utc(
                        int(getattr(item, "time", 0))
                    ),
                    entry_commission=float(getattr(item, "commission", 0.0)),
                )
            )
        return tuple(positions)

    def history_deals(self, since_utc: datetime, magic_number: int) -> tuple[BrokerDeal, ...]:
        raw = self._api.history_deals_get(since_utc)
        if raw is None:
            raise RuntimeError(f"MetaTrader 5 history_deals_get failed: {self._api.last_error()}")
        if not raw:
            return ()
        self.server_utc_offset_hours()
        deals: list[BrokerDeal] = []
        for item in raw:
            item_magic = int(getattr(item, "magic", 0))
            if item_magic != magic_number:
                continue
            side_value = getattr(item, "type", 0)
            side = Side.LONG if side_value in (self._api.ORDER_TYPE_BUY, self._api.ORDER_TYPE_BUY_LIMIT, self._api.ORDER_TYPE_BUY_STOP) else Side.SHORT
            deals.append(
                BrokerDeal(
                    ticket=str(getattr(item, "ticket", "")),
                    position_ticket=str(getattr(item, "position_id", getattr(item, "ticket", ""))),
                    symbol=str(getattr(item, "symbol", "")),
                    side=side,
                    lots=float(getattr(item, "volume", 0.0)),
                    price=float(getattr(item, "price", 0.0)),
                    time_utc=broker_wall_time_to_utc(int(getattr(item, "time", 0))),
                    profit=float(getattr(item, "profit", 0.0)),
                    commission=float(getattr(item, "commission", 0.0)),
                    magic_number=item_magic,
                    comment=str(getattr(item, "comment", "")),
                    is_entry=_deal_is_entry(self._api, getattr(item, "entry", None)),
                )
            )
        return tuple(deals)

    def check(self, request: OrderRequest) -> BrokerResult:
        if not self._is_demo_account():
            return BrokerResult("TRADE_DISABLED", message="Demo account required")
        trade_request = self._order_request_dict(request)
        result = self._api.order_check(trade_request)
        return self._result_from_api(result, request)

    def send_market(self, request: OrderRequest) -> BrokerResult | None:
        if not self._is_demo_account():
            return BrokerResult("TRADE_DISABLED", message="Demo account required")
        trade_request = self._order_request_dict(request)
        try:
            result = self._api.order_send(trade_request)
        except TimeoutError:
            return None
        if result is None:
            return None
        return self._result_from_api(result, request)

    def modify_sl_tp(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult:
        if not self._is_demo_account():
            return BrokerResult("TRADE_DISABLED", message="Demo account required")
        positions = self._api.positions_get(group="", symbol="")
        if positions is None:
            return BrokerResult(
                "UNKNOWN",
                message=f"MetaTrader 5 positions_get failed: {self._api.last_error()}",
            )
        item = next(
            (position for position in positions if str(getattr(position, "ticket", "")) == str(ticket)),
            None,
        )
        if item is None:
            return BrokerResult("POSITION_NOT_FOUND")
        symbol = str(getattr(item, "symbol", ""))
        side_value = getattr(item, "type", 0)
        side = Side.LONG if side_value in (self._api.ORDER_TYPE_BUY, self._api.ORDER_TYPE_BUY_LIMIT, self._api.ORDER_TYPE_BUY_STOP) else Side.SHORT
        lots = float(getattr(item, "volume", 0.0))
        magic_number = int(getattr(item, "magic", 0))
        request = {
            "action": self._api.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(ticket),
            "sl": stop_loss,
            "tp": take_profit,
            "magic": magic_number,
        }
        try:
            result = self._api.order_send(request)
        except TimeoutError:
            return BrokerResult("UNKNOWN")
        return self._result_from_api(
            result,
            OrderRequest(symbol, side, lots, stop_loss, take_profit, magic_number, ticket),
        )

    def close(self, ticket: str) -> BrokerResult:
        info = self._api.account_info()
        if info is None:
            return BrokerResult("UNKNOWN")
        if not self._is_demo_account(info):
            return BrokerResult("TRADE_DISABLED", message="Demo account required")
        self._account_mode = _margin_mode_name(
            getattr(info, "margin_mode", None),
            self._api,
        )
        if self._account_mode == "unknown":
            return BrokerResult("UNKNOWN")

        all_positions = self._api.positions_get(group="", symbol="")
        if all_positions is None:
            return BrokerResult(
                "UNKNOWN",
                message=f"MetaTrader 5 positions_get failed: {self._api.last_error()}",
            )
        item = None
        for position in all_positions:
            if str(getattr(position, "ticket", "")) == str(ticket):
                item = position
                break
        if item is None:
            return BrokerResult("POSITION_NOT_FOUND")
        side = Side.LONG if getattr(item, "type", 0) in (self._api.ORDER_TYPE_BUY, self._api.ORDER_TYPE_BUY_LIMIT, self._api.ORDER_TYPE_BUY_STOP) else Side.SHORT
        close_type = self._api.ORDER_TYPE_SELL if side is Side.LONG else self._api.ORDER_TYPE_BUY
        symbol = str(getattr(item, "symbol", ""))
        lots = float(getattr(item, "volume", 0.0))
        stop_loss = float(getattr(item, "sl", 0.0) or 0.0)
        take_profit = float(getattr(item, "tp", 0.0) or 0.0)
        magic_number = int(getattr(item, "magic", 0))
        close_side = Side.SHORT if side is Side.LONG else Side.LONG
        close_request = OrderRequest(
            symbol,
            close_side,
            lots,
            stop_loss,
            take_profit,
            magic_number,
            ticket,
        )
        price = self._execution_price_for(close_request)
        if price is None:
            return BrokerResult("PRICE_OFF")
        info = self._api.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MetaTrader 5 symbol_info failed for {symbol}: {self._api.last_error()}")
        request = {
            "action": self._api.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": close_type,
            "volume": lots,
            "price": price,
            "magic": magic_number,
            "comment": str(ticket),
            "deviation": self._deviation_points,
            "type_filling": _preferred_fill_mode(self._api, info),
        }
        if self._account_mode == "hedging":
            request["position"] = int(getattr(item, "ticket", ticket))
        try:
            result = self._api.order_send(request)
        except TimeoutError:
            return BrokerResult("UNKNOWN")
        return self._result_from_api(result, close_request)

    def _execution_price_for(self, request: OrderRequest) -> float | None:
        tick = self.tick(request.symbol)
        if tick is None:
            return None
        return tick.ask if request.side is Side.LONG else tick.bid

    def _is_demo_account(self, info: object | None = None) -> bool:
        account_info = self._api.account_info() if info is None else info
        if account_info is None:
            raise RuntimeError(f"MetaTrader 5 account_info failed: {self._api.last_error()}")
        demo_mode = getattr(self._api, "ACCOUNT_TRADE_MODE_DEMO", None)
        trade_mode = getattr(account_info, "trade_mode", None)
        return (
            demo_mode is not None
            and isinstance(trade_mode, int)
            and not isinstance(trade_mode, bool)
            and trade_mode == demo_mode
        )

    def _order_request_dict(self, request: OrderRequest) -> dict[str, Any]:
        price = self._execution_price_for(request)
        if price is None:
            raise RuntimeError(f"No live tick available for {request.symbol}")
        type_value = self._api.ORDER_TYPE_BUY if request.side is Side.LONG else self._api.ORDER_TYPE_SELL
        info = self._api.symbol_info(request.symbol)
        if info is None:
            raise RuntimeError(
                f"MetaTrader 5 symbol_info failed for {request.symbol}: {self._api.last_error()}"
            )
        fill_mode = _preferred_fill_mode(self._api, info)
        comment = str(request.intent_id)
        if not comment.isascii() or not comment.isdigit() or len(comment) > 31:
            raise ValueError("MT5 intent ID must be numeric and at most 31 characters")
        return {
            "action": self._api.TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": request.lots,
            "type": type_value,
            "price": price,
            "sl": request.stop_loss,
            "tp": request.take_profit,
            "deviation": self._deviation_points,
            "magic": request.magic_number,
            "comment": comment,
            "type_filling": fill_mode,
        }

    def _result_from_api(self, result: object | None, request: OrderRequest) -> BrokerResult:
        if result is None:
            return BrokerResult("UNKNOWN")
        retcode = getattr(result, "retcode", None)
        if retcode is None and isinstance(result, dict):
            retcode = result.get("retcode")
        code = _broker_result_code(self._api, retcode)
        price = getattr(result, "price", None)
        if price is None and isinstance(result, dict):
            price = result.get("price")
        ticket = getattr(result, "order", None)
        if ticket is None and isinstance(result, dict):
            ticket = result.get("order")
        lots = getattr(result, "volume", None)
        if lots is None and isinstance(result, dict):
            lots = result.get("volume")
        commission = getattr(result, "commission", None)
        if commission is None and isinstance(result, dict):
            commission = result.get("commission")
        return BrokerResult(
            code=code,
            ticket=str(ticket) if ticket is not None else None,
            price=float(price) if price is not None else None,
            lots=float(lots) if lots is not None else None,
            message=str(getattr(result, "comment", "") or ""),
            commission=float(commission) if commission is not None else None,
        )


def _preferred_fill_mode(api: _MT5API, info: _SymbolInfo) -> int:
    allowed = info.filling_mode
    if allowed & api.SYMBOL_FILLING_IOC:
        return api.ORDER_FILLING_IOC
    if allowed & api.SYMBOL_FILLING_FOK:
        return api.ORDER_FILLING_FOK
    raise RuntimeError("MT5 symbol has no supported market-order filling mode")


def _margin_mode_name(value: object, api: _MT5API) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"hedging", "netting", "exchange"}:
            return lowered
    if isinstance(value, int) and not isinstance(value, bool):
        mapping = {
            getattr(api, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", -1): "netting",
            getattr(api, "ACCOUNT_MARGIN_MODE_EXCHANGE", -1): "exchange",
            getattr(api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", -1): "hedging",
        }
        return mapping.get(value, "unknown")
    return "unknown"


def _deal_is_entry(api: _MT5API, value: object) -> bool | None:
    if value == getattr(api, "DEAL_ENTRY_IN", object()):
        return True
    exit_values = {
        getattr(api, "DEAL_ENTRY_OUT", object()),
        getattr(api, "DEAL_ENTRY_INOUT", object()),
        getattr(api, "DEAL_ENTRY_OUT_BY", object()),
    }
    if value in exit_values:
        return False
    return None


def _broker_result_code(api: _MT5API, retcode: Any) -> str:
    if retcode is None:
        return "UNKNOWN"
    mapping: dict[str, str] = {
        "TRADE_RETCODE_DONE": "DONE",
        "TRADE_RETCODE_INVALID_STOPS": "INVALID_STOPS",
        "TRADE_RETCODE_INVALID_FILL": "INVALID_FILL",
        "TRADE_RETCODE_REQUOTE": "REQUOTE",
        "TRADE_RETCODE_REJECT": "REJECTED",
        "TRADE_RETCODE_REJECTED": "REJECTED",
        "TRADE_RETCODE_NO_MONEY": "NO_MONEY",
        "TRADE_RETCODE_MARKET_CLOSED": "MARKET_CLOSED",
        "TRADE_RETCODE_TIMEOUT": "TIMEOUT",
        "TRADE_RETCODE_PRICE_OFF": "PRICE_OFF",
        "TRADE_RETCODE_CONNECTION": "CONNECTION",
    }
    text_mapping = {
        "DONE": "DONE",
        "INVALID_STOPS": "INVALID_STOPS",
        "INVALID_FILL": "INVALID_FILL",
        "REQUOTE": "REQUOTE",
        "REJECT": "REJECTED",
        "REJECTED": "REJECTED",
        "NO_MONEY": "NO_MONEY",
        "MARKET_CLOSED": "MARKET_CLOSED",
        "TIMEOUT": "TIMEOUT",
        "PRICE_OFF": "PRICE_OFF",
        "CONNECTION": "CONNECTION",
        "UNKNOWN": "UNKNOWN",
    }
    if isinstance(retcode, str):
        named = retcode.upper()
        if named in text_mapping:
            return text_mapping[named]
        if named.startswith("TRADE_RETCODE_") and named in mapping:
            return mapping[named]
    for constant, code in mapping.items():
        value = getattr(api, constant, None)
        if value is not None and retcode == value:
            return code
    return "UNKNOWN"


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
