from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bot.adapters.broker import BrokerAccount, OrderRequest, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import (
    Fault,
    FaultPlan,
    SimBroker,
    SimCosts,
    SimulatedCrash,
)
from bot.adapters.store import StateStore
from bot.app.executor import RETURN_CODE_POLICY, Executor, result_policy
from bot.core.clock import SimClock
from bot.core.types import ExitReason, Side, SymbolSpec

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
SPEC = SymbolSpec(
    "EURUSD", "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
    contract_size=100_000,
)
TICK = Tick(NOW, 1.1, 1.1001)
ACCOUNT = BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging")


def _executor(
    tmp_path,
    fault: str | None = None,
    *,
    broker_commission: float = 0.0,
):
    clock = SimClock(NOW)
    broker = SimBroker(
        ACCOUNT,
        {"EURUSD": SPEC},
        {"EURUSD": TICK},
        clock=clock,
        costs={"EURUSD": SimCosts(commission_per_lot_side=broker_commission)},
        fault_plan=FaultPlan((Fault(1, fault),)) if fault else FaultPlan(),
    )
    store = StateStore(tmp_path / "state.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=1,
        unknown_outcome_wait_s=2,
        code_version="code",
        settings_version="settings",
        commission_per_lot_side={"EURUSD": 3.0},
        wait=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    return broker, store, executor


def _open(executor: Executor):
    return executor.open_trade(
        intent_key="entry-2026-10-26T12:00:00-EURUSD-H1",
        symbol="EURUSD",
        side=Side.LONG,
        lots=0.01,
        entry=1.1001,
        stop_loss=1.099,
        take_profit=1.102,
        actual_risk=1.1,
        strategy="A",
        timeframe="H1",
    )


@pytest.mark.parametrize(
    ("fault", "expected_status", "position_count"),
    [
        ("timeout_unfilled", "FAILED", 0),
        ("timeout_filled", "CONFIRMED", 1),
        ("dropped_response", "CONFIRMED", 1),
        ("invalid_stops", "FAILED", 0),
        ("wrong_fill_mode", "FAILED", 0),
    ],
)
def test_open_trade_resolves_simulated_order_faults(
    tmp_path, fault: str, expected_status: str, position_count: int
) -> None:
    broker, store, executor = _executor(tmp_path, fault)

    result = _open(executor)

    assert (result.trade is not None) is (position_count == 1)
    assert len(broker.positions(712345)) == position_count
    assert all(position.stop_loss > 0 for position in broker.positions(712345))
    intents = store._connection().execute("SELECT status FROM intents").fetchall()
    assert [row["status"] for row in intents] == [expected_status]
    assert len(intents) == 1
    assert len(store.open_trades()) == position_count


def test_crash_after_send_is_resolved_by_a_fresh_executor(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path, "crash_after_send")
    with pytest.raises(SimulatedCrash):
        _open(executor)
    assert store.unresolved_intents()[0].status == "SENT"

    resumed = Executor(
        BrokerGateway(broker, max_retries=0),
        store,
        SimClock(NOW),
        magic_number=712345,
        max_send_retries=1,
        unknown_outcome_wait_s=2,
        code_version="code",
        settings_version="settings",
        commission_per_lot_side={"EURUSD": 3.0},
        wait=lambda _: None,
    )
    result = resumed.resolve_intent(store.unresolved_intents()[0])

    assert result is not None
    assert result.ticket == "1"
    assert len(broker.positions(712345)) == 1
    assert broker.positions(712345)[0].stop_loss > 0
    assert len(store.open_trades()) == 1
    assert store._connection().execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    assert store.open_trades()[0].strategy == "A"
    assert store.open_trades()[0].timeframe == "H1"
    assert store.unresolved_intents() == ()


def test_stop_new_is_checked_inside_the_serialized_order_operation(tmp_path) -> None:
    _, store, executor = _executor(tmp_path)
    executor._gateway.run_serialized(lambda _: store.set_flag("stop_new", True))

    result = _open(executor)

    assert result.trade is None
    assert result.reason == "STOP_NEW"
    assert store.unresolved_intents() == ()
    assert store._connection().execute("SELECT status FROM intents").fetchone()[0] == "FAILED"


def test_disconnect_leaves_unknown_until_a_broker_search_succeeds(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path, "disconnect")

    result = _open(executor)

    assert result.reason == "UNKNOWN_OUTCOME"
    assert store.unresolved_intents()[0].status == "UNKNOWN"
    broker.connected = True
    resolved = executor.resolve_intent(store.unresolved_intents()[0])
    assert resolved is None
    assert broker.positions(712345) == ()
    assert store.unresolved_intents() == ()


def test_return_code_policy_covers_simulator_codes() -> None:
    simulator_codes = {
        "DONE",
        "CONNECTION",
        "INVALID_STOPS",
        "INVALID_FILL",
        "POSITION_NOT_FOUND",
        "PRICE_OFF",
        "INVALID_VOLUME",
        "INVALID_COMMENT",
        "TRADE_DISABLED",
        "UNKNOWN_SYMBOL",
    }

    assert simulator_codes <= RETURN_CODE_POLICY.keys()
    assert result_policy("UNRECOGNIZED") == "UNKNOWN"
    assert result_policy(None) == "UNKNOWN"


def test_close_trade_uses_broker_deal_and_alerts_on_commission_drift(tmp_path) -> None:
    _, store, executor = _executor(tmp_path, broker_commission=0.0)
    opened = _open(executor)
    assert opened.trade is not None

    closed = executor.close_position(opened.trade.ticket, ExitReason.MANUAL)

    assert closed is not None
    assert closed.closed_at_utc == NOW.isoformat()
    assert closed.close_reason == ExitReason.MANUAL.value
    assert closed.commission == 0.0
    assert store.open_trades() == ()
    alerts = store.pending_alerts()
    assert len(alerts) == 1
    assert "Commission drift" in alerts[0].message


def test_reconcile_does_not_confuse_an_entry_deal_with_a_close(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path)
    opened = _open(executor)
    assert opened.trade is not None
    close_intent = store.create_intent(
        intent_key=f"close-{opened.trade.ticket}-pending",
        symbol="EURUSD",
        side=Side.SHORT.value,
        lots=0.01,
        entry=opened.trade.entry,
        stop_loss=opened.trade.stop_loss,
        take_profit=opened.trade.take_profit,
        actual_risk=0.0,
        created_at_utc=NOW.isoformat(),
        code_version="code",
        settings_version="settings",
    )
    store.transition_intent(close_intent.intent_id, "SENT")

    assert executor.resolve_intent(close_intent) is None

    assert store.get_intent(close_intent.intent_id).status == "FAILED"
    assert broker.positions(712345)
    assert store.open_trades()[0].ticket == opened.trade.ticket


def test_reconcile_restores_a_missing_stop_from_the_trade_record(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path)
    opened = _open(executor)
    assert opened.trade is not None
    broker._positions[opened.trade.ticket] = replace(
        broker._positions[opened.trade.ticket], stop_loss=0.0, take_profit=0.0
    )

    report = executor.reconcile()

    assert report.actions == (f"restored-stop:{opened.trade.ticket}",)
    position = broker.positions(712345)[0]
    assert position.stop_loss == opened.trade.stop_loss
    assert position.take_profit == opened.trade.take_profit
    assert store.open_trades()[0].ticket == position.ticket


def test_reconcile_adopts_unrecorded_protected_position(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path)
    external = broker.send_market(
        OrderRequest(
            "EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "manual"
        )
    )
    assert external is not None and external.ticket is not None

    report = executor.reconcile()

    assert report.actions == (f"adopted:{external.ticket}",)
    adopted = store.open_trades()[0]
    assert adopted.ticket == external.ticket
    assert adopted.strategy == "ADOPTED"
    assert broker.positions(712345)[0].stop_loss > 0
    assert any("Adopted broker position" in alert.message for alert in store.pending_alerts())


def test_reconcile_closes_unprotected_position_without_a_recovery_stop(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path)
    external = broker.send_market(
        OrderRequest(
            "EURUSD", Side.LONG, 0.01, 1.099, 1.102, 712345, "manual"
        )
    )
    assert external is not None and external.ticket is not None
    broker._positions[external.ticket] = replace(
        broker._positions[external.ticket], stop_loss=0.0, take_profit=0.0
    )

    report = executor.reconcile()

    assert report.actions == (f"closed-unprotected:{external.ticket}",)
    assert broker.positions(712345) == ()
    assert store.open_trades() == ()


def test_reconcile_closes_store_row_from_exit_deal_when_position_is_gone(tmp_path) -> None:
    broker, store, executor = _executor(tmp_path)
    opened = _open(executor)
    assert opened.trade is not None
    assert broker.close(opened.trade.ticket).code == "DONE"

    report = executor.reconcile()

    assert report.actions == (f"closed-in-store:{opened.trade.ticket}",)
    assert store.open_trades() == ()
    closed = store._connection().execute(
        "SELECT close_reason FROM trades WHERE ticket = ?", (opened.trade.ticket,)
    ).fetchone()
    assert closed["close_reason"] == ExitReason.UNKNOWN.value
