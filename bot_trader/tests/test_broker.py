from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.adapters.broker import (
    BrokerAccount,
    OrderRequest,
    Tick,
)
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import (
    Fault,
    FaultName,
    FaultPlan,
    SimBroker,
    SimCosts,
    SimulatedCrash,
)
from bot.core.clock import SimClock
from bot.core.types import Candle, Side, SymbolSpec

SPEC = SymbolSpec(
    "EURUSD",
    "EUR",
    "USD",
    0.00001,
    0.0001,
    10,
    0.01,
    0.01,
    100,
    contract_size=100_000,
)
TICK = Tick(datetime(2026, 10, 26, 12, tzinfo=UTC), 1.1, 1.1001)
ACCOUNT = BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging")
REQUEST = OrderRequest("EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "1001")


def make_broker(fault: Fault | None = None) -> SimBroker:
    faults = FaultPlan((fault,)) if fault else FaultPlan()
    return SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        fault_plan=faults,
        clock=SimClock(TICK.time_utc),
    )


def test_sim_broker_accepts_a_demo_order_with_protective_stops() -> None:
    broker = make_broker()
    checked = broker.check(REQUEST)
    result = broker.send_market(REQUEST)

    assert checked.code == "DONE"
    assert result is not None
    assert result.code == "DONE"
    assert result.price == TICK.ask
    positions = broker.positions(REQUEST.magic_number)
    assert len(positions) == 1
    assert positions[0].stop_loss == REQUEST.stop_loss
    assert positions[0].take_profit == REQUEST.take_profit
    assert positions[0].comment == REQUEST.intent_id


def test_sim_broker_sells_at_the_bid() -> None:
    broker = make_broker()
    request = OrderRequest("EURUSD", Side.SHORT, 0.01, 1.102, 1.098, 712345, "1002")

    result = broker.send_market(request)

    assert result is not None
    assert result.code == "DONE"
    assert result.price == TICK.bid


def test_sim_broker_applies_configured_slippage_and_commission() -> None:
    broker = SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        clock=SimClock(TICK.time_utc),
        costs={"EURUSD": SimCosts(commission_per_lot_side=3, slippage_points=2)},
    )

    result = broker.send_market(REQUEST)
    assert result is not None
    assert result.price == pytest.approx(TICK.ask + 2 * SPEC.point)
    assert result.commission == pytest.approx(0.03)
    assert broker.account().balance == pytest.approx(2_999.97)


@pytest.mark.parametrize("commission, slippage", [(-1, 0), (0, -1), (float("inf"), 0)])
def test_sim_costs_reject_invalid_values(commission: float, slippage: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        SimCosts(commission_per_lot_side=commission, slippage_points=slippage)


def test_sim_broker_rejects_costs_for_unknown_symbols() -> None:
    with pytest.raises(ValueError, match="unknown symbols"):
        SimBroker(
            ACCOUNT,
            {"EURUSD": SPEC},
            {"EURUSD": TICK},
            clock=SimClock(TICK.time_utc),
            costs={"GBPUSD": SimCosts()},
        )


def test_candle_stop_is_filled_and_precedes_target_when_both_are_hit() -> None:
    broker = SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        clock=SimClock(TICK.time_utc),
        costs={"EURUSD": SimCosts(commission_per_lot_side=3, slippage_points=2)},
    )
    broker.send_market(REQUEST)
    candle = Candle(
        datetime(2026, 10, 26, 13, tzinfo=UTC),
        1.1005,
        1.103,
        1.098,
        1.101,
    )

    results = broker.process_candle("EURUSD", candle)

    assert len(results) == 1
    assert results[0].price == pytest.approx(REQUEST.stop_loss - 2 * SPEC.point)
    assert broker.positions(REQUEST.magic_number) == ()
    deals = broker.history_deals(datetime(2026, 10, 26, tzinfo=UTC), REQUEST.magic_number)
    assert [deal.profit for deal in deals] == [0.0, pytest.approx(-1.14)]
    assert broker.account().balance == pytest.approx(2_998.80)


def test_candle_target_exit_uses_target_price_with_slippage_and_costs() -> None:
    broker = SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        clock=SimClock(TICK.time_utc),
        costs={"EURUSD": SimCosts(commission_per_lot_side=3, slippage_points=2)},
    )
    broker.send_market(REQUEST)
    candle = Candle(
        datetime(2026, 10, 26, 13, tzinfo=UTC),
        1.1005,
        1.1025,
        1.1002,
        1.1015,
    )

    results = broker.process_candle("EURUSD", candle)

    assert len(results) == 1
    assert results[0].price == pytest.approx(REQUEST.take_profit - 2 * SPEC.point)
    assert broker.account().balance == pytest.approx(3_001.80)


def test_sim_broker_rejects_duplicate_and_out_of_order_candles() -> None:
    broker = make_broker()
    broker.send_market(REQUEST)
    candle = Candle(
        datetime(2026, 10, 26, 13, tzinfo=UTC),
        1.1005,
        1.101,
        1.1002,
        1.1008,
    )

    assert broker.process_candle("EURUSD", candle) == ()
    with pytest.raises(ValueError, match="strictly increasing"):
        broker.process_candle("EURUSD", candle)
    earlier = Candle(
        datetime(2026, 10, 26, 12, 30, tzinfo=UTC),
        1.1005,
        1.101,
        1.1002,
        1.1008,
    )
    with pytest.raises(ValueError, match="precede the simulator clock"):
        broker.process_candle("EURUSD", earlier)


def test_gateway_exposes_terminal_account_margin_and_server_time() -> None:
    gateway = BrokerGateway(make_broker())

    assert gateway.terminal_info().connected
    assert gateway.terminal_info().trade_allowed
    assert gateway.server_utc_offset_hours() == 3
    assert gateway.calculate_margin(REQUEST) == pytest.approx(11.001)


def test_duplicate_candle_fault_is_deterministic() -> None:
    candle = Candle(
        datetime(2026, 10, 26, 12, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )
    broker = SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        clock=SimClock(TICK.time_utc),
        candle_data={("EURUSD", "H1"): (candle,)},
        fault_plan=FaultPlan((Fault(1, "duplicate_candle", "candles"),)),
    )

    assert broker.candles("EURUSD", "H1") == (candle, candle)
    assert broker.candles("EURUSD", "H1") == (candle,)


@pytest.mark.parametrize("fault_name", ["timeout_unfilled", "timeout_filled", "dropped_response"])
def test_simulated_unknown_outcomes_are_reconcilable(fault_name: FaultName) -> None:
    broker = make_broker(Fault(1, fault_name))
    result = broker.send_market(REQUEST)
    positions = broker.positions(REQUEST.magic_number)

    assert result is None
    expected_positions = 0 if fault_name == "timeout_unfilled" else 1
    assert len(positions) == expected_positions


def test_crash_after_order_leaves_a_position_for_restart_reconciliation() -> None:
    broker = make_broker(Fault(1, "crash_after_send"))
    with pytest.raises(SimulatedCrash):
        broker.send_market(REQUEST)
    assert len(broker.positions(REQUEST.magic_number)) == 1


def test_stale_tick_fault_is_deterministic() -> None:
    broker = make_broker(Fault(1, "stale_tick", "tick"))
    assert broker.tick("EURUSD") is None
    assert broker.tick("EURUSD") == TICK


@pytest.mark.parametrize(
    ("fault_name", "expected_code"),
    [("invalid_stops", "INVALID_STOPS"), ("wrong_fill_mode", "INVALID_FILL")],
)
def test_simulated_rejections_return_explicit_broker_codes(
    fault_name: FaultName, expected_code: str
) -> None:
    broker = make_broker(Fault(1, fault_name))
    result = broker.send_market(REQUEST)
    assert result is not None
    assert result.code == expected_code
    assert broker.positions(REQUEST.magic_number) == ()


def test_simulated_disconnect_requires_reconnect() -> None:
    broker = make_broker(Fault(1, "disconnect"))
    result = broker.send_market(REQUEST)
    assert result is not None and result.code == "CONNECTION"
    with pytest.raises(ConnectionError):
        broker.positions(REQUEST.magic_number)
    assert broker.connect()
    assert broker.positions(REQUEST.magic_number) == ()


def test_simulator_refuses_a_non_demo_account() -> None:
    live = BrokerAccount(1, "Live", "USD", 3_000, 3_000, False, True, "hedging")
    with pytest.raises(ValueError, match="demo-account"):
        SimBroker(live, {"EURUSD": SPEC}, {"EURUSD": TICK}, SimClock(TICK.time_utc))
