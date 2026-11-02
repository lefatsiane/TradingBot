from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from bot.adapters.mt5_broker import MT5ReadOnlyBroker


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


class FakeMT5:
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
    assert candles[0].time == datetime.fromtimestamp(1_767_620_400, tz=UTC)
    assert tick is not None
    assert tick.time_utc == datetime.fromtimestamp(1_767_620_400.25, tz=UTC)
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


def test_mt5_adapter_rejects_bad_timeframe_and_missing_broker_offset() -> None:
    broker = MT5ReadOnlyBroker(api=FakeMT5())

    with pytest.raises(ValueError, match="timeframe"):
        broker.candles("EURUSD", "M15")
    with pytest.raises(RuntimeError, match="does not expose"):
        broker.server_utc_offset_hours()


def test_mt5_adapter_uses_explicit_broker_offset_provider() -> None:
    broker = MT5ReadOnlyBroker(api=FakeMT5(), server_offset_provider=lambda: 2)

    assert broker.server_utc_offset_hours() == 2


@pytest.mark.parametrize("offset", [-13, 15, True, 2.5])
def test_mt5_adapter_rejects_invalid_reported_server_offset(offset: object) -> None:
    broker = MT5ReadOnlyBroker(
        api=FakeMT5(),
        server_offset_provider=lambda: offset,
    )

    with pytest.raises(ValueError, match="offset"):
        broker.server_utc_offset_hours()


def test_mt5_adapter_rejects_naive_history_range() -> None:
    with pytest.raises(ValueError, match="UTC"):
        MT5ReadOnlyBroker(
            api=FakeMT5(),
            history_start_utc=datetime(2026, 1, 1),
            history_end_utc=datetime(2026, 2, 1, tzinfo=UTC),
        )
