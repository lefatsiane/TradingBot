from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import bot.adapters.mt5_broker as mt5_module
from bot.adapters.broker import OrderRequest
from bot.adapters.mt5_broker import MT5Broker, MT5ReadOnlyBroker
from bot.core.types import Side

_MT5_PACKAGE_FUNCTIONS = frozenset(
    {
        "initialize",
        "login",
        "shutdown",
        "version",
        "last_error",
        "account_info",
        "terminal_info",
        "symbols_total",
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "symbol_select",
        "market_book_add",
        "market_book_get",
        "market_book_release",
        "copy_rates_from",
        "copy_rates_from_pos",
        "copy_rates_range",
        "copy_ticks_from",
        "copy_ticks_range",
        "orders_total",
        "orders_get",
        "order_calc_margin",
        "order_calc_profit",
        "order_check",
        "order_send",
        "positions_total",
        "positions_get",
        "history_orders_total",
        "history_orders_get",
        "history_deals_total",
        "history_deals_get",
    }
)


class StrictFakeMT5:
    def __getattr__(self, name: str) -> object:
        if name not in _MT5_PACKAGE_FUNCTIONS:
            raise AssertionError(f"Unsupported MetaTrader5 API name: {name}")
        raise AssertionError(f"Fake MetaTrader5 API function is not implemented: {name}")


@dataclass(frozen=True)
class SymbolRecord:
    currency_base: str = "EUR"
    currency_profit: str = "USD"
    point: float = 0.00001
    trade_tick_size: float = 0.0001
    trade_tick_value_loss: float = 10.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    digits: int = 5
    trade_contract_size: float = 100_000.0


@dataclass(frozen=True)
class TickRecord:
    time: int = 1_767_620_400
    time_msc: int = 1_767_620_400_250
    bid: float = 1.1
    ask: float = 1.1001


class FakeMT5(StrictFakeMT5):
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240

    def __init__(self) -> None:
        self.rates = [
            {
                "time": 1_767_620_400,
                "open": 1.1,
                "high": 1.101,
                "low": 1.099,
                "close": 1.1005,
                "spread": 10,
                "tick_volume": 123,
            }
        ]
        self.range_requests: list[tuple[str, int, datetime, datetime]] = []

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple[int, str]:
        return (0, "success")

    def terminal_info(self) -> object:
        return object()

    def symbol_info(self, symbol: str) -> SymbolRecord:
        assert symbol == "EURUSD"
        return SymbolRecord()

    def symbol_info_tick(self, symbol: str) -> TickRecord:
        assert symbol == "EURUSD"
        return TickRecord()

    def copy_rates_range(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, int | float]]:
        self.range_requests.append((symbol, timeframe, date_from, date_to))
        return self.rates


def test_mt5_adapter_maps_symbol_candles_spread_and_tick_to_utc() -> None:
    api = FakeMT5()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 1, tzinfo=UTC)
    broker = MT5ReadOnlyBroker(
        api=api,
        history_start_utc=start,
        history_end_utc=end,
    )

    spec = broker.symbol_info("EURUSD")
    candles = broker.candles("EURUSD", "H1")
    tick = broker.tick("EURUSD")

    assert spec.base_currency == "EUR"
    assert spec.quote_currency == "USD"
    assert spec.contract_size == 100_000
    assert candles[0].spread == pytest.approx(0.0001)
    assert candles[0].tick_volume == 123
    assert candles[0].time == datetime.fromtimestamp(
        1_767_620_400 - 2 * 60 * 60,
        tz=UTC,
    )
    assert tick is not None
    assert tick.time_utc == datetime.fromtimestamp(
        1_767_620_400.25 - 2 * 60 * 60,
        tz=UTC,
    )
    assert api.range_requests == [("EURUSD", api.TIMEFRAME_H1, start, end)]


def test_mt5_adapter_fetches_long_ranges_in_bounded_chunks() -> None:
    api = FakeMT5()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=200)
    broker = MT5ReadOnlyBroker(
        api=api,
        history_start_utc=start,
        history_end_utc=end,
    )

    candles = broker.candles("EURUSD", "H1")

    assert len(api.range_requests) == 2
    assert api.range_requests[0][2:] == (start, start + timedelta(days=180))
    assert api.range_requests[1][2:] == (start + timedelta(days=180), end)
    assert len(candles) == 1


def test_mt5_adapter_rejects_bad_timeframe() -> None:
    broker = MT5ReadOnlyBroker(
        api=FakeMT5(),
        clock=lambda: datetime(2026, 1, 5, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="timeframe"):
        broker.candles("EURUSD", "M15")


@pytest.mark.parametrize(
    ("utc_time", "expected_offset"),
    [
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 3),
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 2),
        (datetime(2027, 3, 14, 12, tzinfo=UTC), 3),
    ],
)
def test_mt5_readonly_adapter_uses_new_york_server_rule(
    utc_time: datetime,
    expected_offset: int,
) -> None:
    broker = MT5ReadOnlyBroker(api=FakeMT5(), clock=lambda: utc_time)

    assert broker.server_utc_offset_hours() == expected_offset


def test_mt5_adapter_rejects_naive_history_range() -> None:
    with pytest.raises(ValueError, match="UTC"):
        MT5ReadOnlyBroker(
            api=FakeMT5(),
            history_start_utc=datetime(2026, 1, 1),
            history_end_utc=datetime(2026, 2, 1, tzinfo=UTC),
        )


class FakeLiveMT5API(StrictFakeMT5):
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    ORDER_TIME_GTC = 0
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_EXCHANGE = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_INVALID_STOPS = 10016
    TRADE_RETCODE_INVALID_FILL = 10030
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_REJECTED = 10006
    TRADE_RETCODE_NO_MONEY = 10019
    TRADE_RETCODE_MARKET_CLOSED = 10018
    TRADE_RETCODE_TIMEOUT = 10012
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_CONNECTION = 10031
    TRADE_RETCODE_UNKNOWN = 10008
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_INOUT = 2
    DEAL_ENTRY_OUT_BY = 3

    def __init__(
        self,
        *,
        order_retcode: int = 10009,
        hedge_close: bool = True,
        timeout_on_send: bool = False,
        position_type: int = 0,
    ) -> None:
        self.order_retcode = order_retcode
        self.hedge_close = hedge_close
        self.timeout_on_send = timeout_on_send
        self.position_type = position_type
        self.last_order: dict[str, object] | None = None
        self.deals: list[SimpleNamespace] = []
        self.initialize_result = True
        self.login_result = True
        self.login_args: tuple[int, str, str] | None = None
        self.shutdown_calls = 0
        self.order_check_calls = 0
        self.order_send_calls = 0
        self.history_calls: list[tuple[datetime, datetime | None]] = []
        self.margin_args: tuple[int, str, float, float] | None = None
        self.positions_result: list[SimpleNamespace] | None = None
        self.history_result: list[SimpleNamespace] | None = None
        self.symbol_select_result = True
        self._tick = SimpleNamespace(
            time=1_767_620_400,
            time_msc=1_767_620_400_250,
            bid=1.1,
            ask=1.1001,
        )
        self.tick_responses: list[SimpleNamespace] = []
        self.tick_requests = 0
        self._info = SimpleNamespace(
            currency_base="EUR",
            currency_profit="USD",
            point=0.00001,
            trade_tick_size=0.0001,
            trade_tick_value_loss=10.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            digits=5,
            trade_contract_size=100_000.0,
            filling_mode=self.SYMBOL_FILLING_IOC,
        )
        self._account = SimpleNamespace(
            login=123456,
            server="ICMarkets-Demo",
            currency="USD",
            balance=3000.0,
            equity=3000.0,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            trade_allowed=True,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
            leverage=100,
        )

    def initialize(self) -> bool:
        return self.initialize_result

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def login(self, login: int, password: str, server: str) -> bool:
        self.login_args = (login, password, server)
        return self.login_result

    def last_error(self) -> tuple[int, str]:
        return (0, "success")

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=True, trade_allowed=True, name="MT5", company="MetaQuotes")

    def account_info(self) -> SimpleNamespace:
        return self._account

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        assert symbol == "EURUSD"
        return self._info

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return self.symbol_select_result

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        assert symbol == "EURUSD"
        self.tick_requests += 1
        if self.tick_responses:
            return self.tick_responses.pop(0)
        return self._tick

    def copy_rates_range(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, float | int]]:
        return [{
            "time": int(date_from.timestamp()),
            "open": 1.1,
            "high": 1.101,
            "low": 1.099,
            "close": 1.1005,
            "spread": 10,
            "tick_volume": 100,
        }]

    def positions_get(self, group: str = "", symbol: str = "") -> list[SimpleNamespace]:
        if self.positions_result is not None:
            return self.positions_result
        if self.hedge_close:
            return [
                SimpleNamespace(
                    ticket=42,
                    magic=712345,
                    symbol="EURUSD",
                    type=self.position_type,
                    volume=0.01,
                    price_open=1.10,
                    sl=1.099,
                    tp=1.102,
                    comment="1001",
                    time=1_767_620_400,
                    commission=0.0,
                )
            ]
        return []

    def history_deals_get(
        self,
        date_from: datetime,
        date_to: datetime | None = None,
        group: str = "",
        symbol: str = "",
    ) -> list[SimpleNamespace]:
        self.history_calls.append((date_from, date_to))
        if self.history_result is not None:
            return self.history_result
        return self.deals

    def order_calc_margin(
        self,
        action: int,
        symbol: str,
        volume: float,
        price: float,
    ) -> float:
        self.margin_args = (action, symbol, volume, price)
        return 12.34

    def order_check(self, request: dict[str, object]) -> SimpleNamespace:
        self.order_check_calls += 1
        self.last_order = request
        return SimpleNamespace(
            retcode=self.order_retcode,
            order=1234,
            price=request["price"],
            volume=request["volume"],
            comment=request["comment"],
            commission=0.0,
        )

    def order_send(self, request: dict[str, object]) -> SimpleNamespace:
        self.order_send_calls += 1
        if self.timeout_on_send:
            raise TimeoutError("simulated MT5 send timeout")
        self.last_order = request
        return SimpleNamespace(
            retcode=self.order_retcode,
            order=1234,
            price=request.get("price"),
            volume=request.get("volume"),
            comment=request.get("comment"),
            commission=0.0,
        )


def test_mt5_broker_uses_live_tick_for_order_checks_and_fill_mode() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api, deviation_points=20)

    result = broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001"))

    assert result.code == "DONE"
    assert result.ticket == "1234"
    assert result.price == pytest.approx(api._tick.ask)
    assert result.lots == pytest.approx(0.01)
    assert result.commission == pytest.approx(0.0)
    assert api.last_order is not None
    assert api.last_order["type_filling"] == api.ORDER_FILLING_IOC
    assert api.last_order["comment"] == "1001"
    assert api.last_order["deviation"] == 20


def test_mt5_broker_uses_fok_only_when_symbol_allows_only_fok() -> None:
    api = FakeLiveMT5API()
    api._info.filling_mode = api.SYMBOL_FILLING_FOK
    broker = MT5Broker(api=api)

    broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001"))

    assert api.last_order is not None
    assert api.last_order["type_filling"] == api.ORDER_FILLING_FOK


def test_mt5_broker_rejects_symbol_with_no_supported_fill_mode() -> None:
    api = FakeLiveMT5API()
    api._info.filling_mode = 0
    broker = MT5Broker(api=api)

    with pytest.raises(RuntimeError, match="filling mode"):
        broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001"))


@pytest.mark.parametrize(
    ("retcode_name", "expected"),
    [
        ("TRADE_RETCODE_DONE", "DONE"),
        ("TRADE_RETCODE_INVALID_STOPS", "INVALID_STOPS"),
        ("TRADE_RETCODE_INVALID_FILL", "INVALID_FILL"),
        ("TRADE_RETCODE_REQUOTE", "REQUOTE"),
        ("TRADE_RETCODE_REJECT", "REJECTED"),
        ("TRADE_RETCODE_NO_MONEY", "NO_MONEY"),
        ("TRADE_RETCODE_MARKET_CLOSED", "MARKET_CLOSED"),
        ("TRADE_RETCODE_TIMEOUT", "TIMEOUT"),
        ("TRADE_RETCODE_PRICE_OFF", "PRICE_OFF"),
        ("TRADE_RETCODE_CONNECTION", "CONNECTION"),
    ],
)
def test_mt5_broker_maps_retcode_constants(retcode_name: str, expected: str) -> None:
    api = FakeLiveMT5API(order_retcode=0)
    api.order_retcode = getattr(api, retcode_name)
    broker = MT5Broker(api=api)

    result = broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001"))

    assert result.code == expected


def test_mt5_broker_rejects_intent_ids_that_cannot_be_preserved_in_comment() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api)

    with pytest.raises(ValueError, match="numeric.*31"):
        broker.check(
            OrderRequest(
                "EURUSD",
                Side.LONG,
                0.01,
                1.099,
                1.102,
                712345,
                "1" * 32,
            )
        )


def test_mt5_broker_preserves_maximum_length_intent_comment() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api)
    intent_id = "1" * 31

    broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, intent_id))

    assert api.last_order is not None
    assert api.last_order["comment"] == intent_id
    assert len(str(api.last_order["comment"])) == 31


def test_mt5_broker_maps_unrecognized_retcode_to_unknown() -> None:
    api = FakeLiveMT5API(order_retcode=99999)
    broker = MT5Broker(api=api)

    result = broker.check(OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001"))

    assert result.code == "UNKNOWN"


def test_mt5_broker_returns_unknown_for_send_timeout() -> None:
    api = FakeLiveMT5API(timeout_on_send=True)
    broker = MT5Broker(api=api)

    result = broker.send_market(
        OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001")
    )

    assert result is None


def test_mt5_broker_connect_reads_password_from_caller_selected_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeLiveMT5API()
    monkeypatch.setenv("TEST_MT5_PASSWORD", "test-password")
    broker = MT5Broker(api=api)

    assert broker.connect(
        login=123456,
        server="Demo-Server",
        password_env_var="TEST_MT5_PASSWORD",
    )
    broker.shutdown()

    assert api.login_args == (123456, "test-password", "Demo-Server")
    assert api.shutdown_calls == 1


@pytest.mark.parametrize(
    ("trade_mode", "is_demo"),
    [
        (FakeLiveMT5API.ACCOUNT_TRADE_MODE_DEMO, True),
        (FakeLiveMT5API.ACCOUNT_TRADE_MODE_CONTEST, False),
        (FakeLiveMT5API.ACCOUNT_TRADE_MODE_REAL, False),
        (False, False),
    ],
)
def test_mt5_broker_identifies_demo_account_from_mt5_trade_mode(
    trade_mode: int,
    is_demo: bool,
) -> None:
    api = FakeLiveMT5API()
    api._account.trade_mode = trade_mode

    account = MT5Broker(api=api).account()

    assert account.is_demo is is_demo


@pytest.mark.parametrize(
    ("margin_mode", "expected"),
    [
        (FakeLiveMT5API.ACCOUNT_MARGIN_MODE_RETAIL_NETTING, "netting"),
        (FakeLiveMT5API.ACCOUNT_MARGIN_MODE_EXCHANGE, "exchange"),
        (FakeLiveMT5API.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING, "hedging"),
    ],
)
def test_mt5_broker_maps_mt5_margin_modes(
    margin_mode: int,
    expected: str,
) -> None:
    api = FakeLiveMT5API()
    api._account.margin_mode = margin_mode

    account = MT5Broker(api=api).account()

    assert account.margin_mode == expected


def test_mt5_broker_fails_closed_when_account_safety_fields_are_missing() -> None:
    api = FakeLiveMT5API()
    del api._account.trade_mode
    del api._account.trade_allowed
    del api._account.margin_mode

    account = MT5Broker(api=api).account()

    assert not account.is_demo
    assert not account.trade_allowed
    assert account.margin_mode == "unknown"


def test_mt5_broker_calculates_margin_with_mt5_market_price() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api)
    request = OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001")

    margin = broker.calculate_margin(request)

    assert margin == pytest.approx(12.34)
    assert api.margin_args == (
        api.TRADE_ACTION_DEAL,
        "EURUSD",
        0.01,
        api._tick.ask,
    )


def test_mt5_broker_reads_terminal_symbol_and_tick() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api)

    terminal = broker.terminal_info()
    symbol = broker.symbol_info("EURUSD")
    tick = broker.tick("EURUSD")
    candles = broker.candles("EURUSD", "H1")

    assert terminal.connected
    assert terminal.trade_allowed
    assert symbol.name == "EURUSD"
    assert tick is not None
    assert tick.bid == api._tick.bid
    assert tick.ask == api._tick.ask
    assert candles


def test_fake_mt5_rejects_functions_not_in_the_real_package_api() -> None:
    unsupported_name = "not_a_real_mt5_function"
    with pytest.raises(AssertionError, match="Unsupported MetaTrader5 API name"):
        getattr(FakeLiveMT5API(), unsupported_name)


def test_mt5_broker_returns_empty_reads_without_requiring_market_tick() -> None:
    api = FakeLiveMT5API(hedge_close=False)
    broker = MT5Broker(api=api)

    assert broker.positions(712345) == ()
    assert broker.history_deals(datetime(2026, 1, 1, tzinfo=UTC), 712345) == ()


def test_mt5_broker_surfaces_position_and_history_read_errors() -> None:
    api = FakeLiveMT5API()
    api.positions_result = None
    api.history_result = None
    broker = MT5Broker(api=api)
    api.positions_get = lambda **_: None
    api.history_deals_get = lambda *_args, **_kwargs: None

    with pytest.raises(RuntimeError, match="positions_get failed"):
        broker.positions(712345)
    with pytest.raises(RuntimeError, match="history_deals_get failed"):
        broker.history_deals(datetime(2026, 1, 1, tzinfo=UTC), 712345)


def test_mt5_broker_does_not_treat_failed_position_reads_as_missing_positions() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api)
    api.positions_get = lambda **_: None

    modify = broker.modify_sl_tp("42", 1.0995, 1.1025)
    close = broker.close("42")

    assert modify.code == "UNKNOWN"
    assert close.code == "UNKNOWN"
    assert api.order_send_calls == 0


def test_mt5_broker_surfaces_failed_symbol_selection() -> None:
    api = FakeLiveMT5API()
    api.symbol_select_result = False

    with pytest.raises(RuntimeError, match="symbol_select failed"):
        MT5Broker(api=api).symbol_info("EURUSD")


def test_mt5_broker_shuts_down_terminal_after_login_failure() -> None:
    api = FakeLiveMT5API()
    api.login_result = False
    broker = MT5Broker(api=api)

    with pytest.raises(ConnectionError, match="login failed"):
        broker.connect(login=123456, server="Demo-Server", password="not-a-real-secret")

    assert api.shutdown_calls == 1


def test_mt5_broker_refuses_non_demo_login_and_shuts_down() -> None:
    api = FakeLiveMT5API()
    api._account.trade_mode = api.ACCOUNT_TRADE_MODE_REAL
    broker = MT5Broker(api=api)

    with pytest.raises(PermissionError, match="demo"):
        broker.connect(login=123456, server="Demo-Server", password="placeholder")

    assert api.shutdown_calls == 1


def test_mt5_broker_never_checks_or_sends_orders_on_non_demo_account() -> None:
    api = FakeLiveMT5API()
    api._account.trade_mode = api.ACCOUNT_TRADE_MODE_REAL
    broker = MT5Broker(api=api)
    request = OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001")

    check = broker.check(request)
    send = broker.send_market(request)
    modify = broker.modify_sl_tp("42", 1.0995, 1.1025)
    close = broker.close("42")

    assert check.code == "TRADE_DISABLED"
    assert send is not None and send.code == "TRADE_DISABLED"
    assert modify.code == "TRADE_DISABLED"
    assert close.code == "TRADE_DISABLED"
    assert api.order_check_calls == 0
    assert api.order_send_calls == 0


def test_mt5_broker_close_uses_position_field_for_hedging_accounts() -> None:
    api = FakeLiveMT5API(hedge_close=True)
    broker = MT5Broker(api=api, deviation_points=10)

    result = broker.close("42")

    assert result.code == "DONE"
    assert api.last_order is not None
    assert "position" in api.last_order
    assert api.last_order["position"] == 42
    assert api.last_order["type"] == api.ORDER_TYPE_SELL
    assert api.last_order["price"] == api._tick.bid


def test_mt5_broker_netting_close_omits_position_field() -> None:
    api = FakeLiveMT5API(hedge_close=True)
    api._account.margin_mode = 0
    broker = MT5Broker(api=api, deviation_points=10)

    result = broker.close("42")

    assert result.code == "DONE"
    assert api.last_order is not None
    assert "position" not in api.last_order
    assert api.last_order["type"] == api.ORDER_TYPE_SELL
    assert api.last_order["price"] == api._tick.bid


def test_mt5_broker_closes_short_at_ask() -> None:
    api = FakeLiveMT5API(position_type=FakeLiveMT5API.ORDER_TYPE_SELL)
    broker = MT5Broker(api=api, deviation_points=10)

    result = broker.close("42")

    assert result.code == "DONE"
    assert api.last_order is not None
    assert api.last_order["type"] == api.ORDER_TYPE_BUY
    assert api.last_order["price"] == api._tick.ask


def test_mt5_broker_modifies_stops_for_the_requested_position() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api, deviation_points=10)

    result = broker.modify_sl_tp("42", 1.0995, 1.1025)

    assert result.code == "DONE"
    assert api.last_order is not None
    assert api.last_order["action"] == api.TRADE_ACTION_SLTP
    assert api.last_order["symbol"] == "EURUSD"
    assert api.last_order["position"] == 42
    assert api.last_order["sl"] == 1.0995
    assert api.last_order["tp"] == 1.1025


def test_mt5_broker_converts_position_server_time_using_reported_offset() -> None:
    api = FakeLiveMT5API()
    broker = MT5Broker(api=api, server_clock_sanity_provider=lambda: 2)

    positions = broker.positions(712345)

    assert len(positions) == 1
    assert positions[0].opened_at_utc == datetime.fromtimestamp(
        1_767_620_400 - 2 * 60 * 60,
        tz=UTC,
    )


def test_mt5_broker_converts_deal_server_time_using_reported_offset() -> None:
    api = FakeLiveMT5API(hedge_close=False)
    api.deals.append(
        SimpleNamespace(
            ticket=51,
            position_id=42,
            symbol="EURUSD",
            type=api.ORDER_TYPE_BUY,
            volume=0.01,
            price=1.1,
            time=1_767_620_400,
            profit=2.0,
            commission=-0.1,
            magic=712345,
            comment="1001",
        )
    )
    broker = MT5Broker(api=api, server_clock_sanity_provider=lambda: 2)

    deals = broker.history_deals(datetime(2026, 1, 1, tzinfo=UTC), 712345)

    assert len(deals) == 1
    assert deals[0].time_utc == datetime.fromtimestamp(
        1_767_620_400 - 2 * 60 * 60,
        tz=UTC,
    )
    assert api.history_calls == [(datetime(2026, 1, 1, tzinfo=UTC), None)]


def test_mt5_broker_infers_offset_from_fresh_server_tick() -> None:
    api = FakeLiveMT5API()
    now = datetime(2026, 1, 5, 13, 40, tzinfo=UTC)
    server_time_msc = int((now.timestamp() + 2 * 60 * 60) * 1_000)
    api.tick_responses = [
        SimpleNamespace(
            time=server_time_msc // 1_000,
            time_msc=server_time_msc + index * 250,
            bid=1.1,
            ask=1.1001,
        )
        for index in range(3)
    ]
    broker = MT5Broker(api=api, clock=lambda: now)

    assert broker.server_utc_offset_hours() == 2


def test_mt5_broker_reports_stale_sanity_tick_without_changing_offset(monkeypatch) -> None:
    api = FakeLiveMT5API()
    now = datetime(2026, 1, 5, 13, 40, tzinfo=UTC)
    server_epoch = int(now.timestamp()) + 2 * 60 * 60 + 61
    api._tick = SimpleNamespace(
        time=server_epoch,
        time_msc=server_epoch * 1000,
        bid=1.1,
        ask=1.1001,
    )
    mismatches: list[str] = []
    broker = MT5Broker(
        api=api,
        clock=lambda: now,
        on_server_clock_mismatch=mismatches.append,
    )
    monkeypatch.setattr(mt5_module.time, "sleep", lambda _: None)

    assert broker.server_utc_offset_hours() == 2
    assert len(mismatches) == 1
    assert "stale" in mismatches[0]


def test_mt5_broker_uses_new_york_rule_and_advancing_ticks_for_sanity() -> None:
    api = FakeLiveMT5API()
    now = datetime(2026, 1, 5, 13, 40, tzinfo=UTC)
    server_time_msc = int((now.timestamp() + 2 * 60 * 60) * 1_000)
    api.tick_responses = [
        SimpleNamespace(
            time=server_time_msc // 1_000,
            time_msc=server_time_msc + index * 250,
            bid=1.1,
            ask=1.1001,
        )
        for index in range(3)
    ]
    mismatches: list[str] = []
    broker = MT5Broker(
        api=api,
        clock=lambda: now,
        on_server_clock_mismatch=mismatches.append,
    )

    assert broker.server_utc_offset_hours() == 2
    assert mismatches == []
    assert api.tick_requests == 3


def test_mt5_broker_keeps_new_york_offset_and_reports_tick_mismatch() -> None:
    api = FakeLiveMT5API()
    now = datetime(2026, 1, 5, 13, 40, tzinfo=UTC)
    server_time_msc = int((now.timestamp() + 3 * 60 * 60) * 1_000)
    api.tick_responses = [
        SimpleNamespace(
            time=server_time_msc // 1_000,
            time_msc=server_time_msc + index * 250,
            bid=1.1,
            ask=1.1001,
        )
        for index in range(3)
    ]
    mismatches: list[str] = []
    broker = MT5Broker(
        api=api,
        clock=lambda: now,
        on_server_clock_mismatch=mismatches.append,
    )

    assert broker.server_utc_offset_hours() == 2
    assert len(mismatches) == 1
    assert "UTC+3" in mismatches[0]


@pytest.mark.parametrize("offset", [-13, 15, True, 2.5])
def test_mt5_broker_keeps_expected_offset_for_invalid_sanity_value(
    offset: object,
) -> None:
    mismatches: list[str] = []
    broker = MT5Broker(
        api=FakeLiveMT5API(),
        server_clock_sanity_provider=lambda: offset,
        on_server_clock_mismatch=mismatches.append,
        clock=lambda: datetime(2026, 1, 5, 13, 40, tzinfo=UTC),
    )

    assert broker.server_utc_offset_hours() == 2
    assert len(mismatches) == 1
