from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from bot.adapters.broker import BrokerAccount, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import SimBroker
from bot.adapters.store import StateStore
from bot.app.control import ControlHandler
from bot.app.executor import Executor
from bot.core.clock import SimClock
from bot.core.types import Side, SymbolSpec

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
SPEC = SymbolSpec(
    "EURUSD", "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
    contract_size=100_000,
)


def _broker_control(tmp_path, *, lock_timeout_s: float = 0.1):
    clock = SimClock(NOW)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
    )
    store = StateStore(tmp_path / "control-broker.db")
    gateway = BrokerGateway(
        broker,
        lock_timeout_s=lock_timeout_s,
        max_retries=0,
    )
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="test",
        settings_version="test",
        wait=lambda _: None,
    )
    control = ControlHandler(
        store,
        clock,
        gateway=gateway,
        executor=executor,
        magic_number=712345,
    )
    return broker, store, gateway, executor, control


def _open_position(executor: Executor) -> str:
    result = executor.open_trade(
        intent_key="control-test",
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
    assert result.trade is not None
    return result.trade.ticket


def test_stop_new_and_resume_only_clear_manual_entry_block(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    assert control.handle("/stop_new") == "New entries stopped."
    assert store.get_flag("stop_new")
    assert control.handle("/resume") == "Entries resumed."
    assert not store.get_flag("stop_new")


def test_resume_refuses_while_any_safety_halt_is_set(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)
    store.set_flag("stop_new", True)
    store.set_flag("halted_daily", True)

    reply = control.handle("/resume")

    assert "cannot resume" in reply.lower()
    assert store.get_flag("stop_new")
    assert store.get_flag("halted_daily")


def test_close_all_requires_yes_within_30_seconds(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    assert control.handle("/close_all") == "Reply YES within 30 seconds to confirm."
    assert not store.get_flag("close_all_pending")
    clock.advance(timedelta(seconds=29))
    assert control.handle("YES") == "Close-all queued; entries stopped."
    assert store.get_flag("stop_new")
    assert store.get_flag("close_all_pending")


def test_close_all_confirmation_expires(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)

    control.handle("/close_all")
    clock.advance(timedelta(seconds=30))

    assert control.handle("YES") == "Confirmation expired."
    assert not store.get_flag("close_all_pending")
    assert not store.get_flag("stop_new")


def test_status_reports_flags_and_unknown_commands_are_rejected(tmp_path) -> None:
    store = StateStore(tmp_path / "control.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    control = ControlHandler(store, clock)
    store.set_flag("stop_new", True)

    assert "stop_new=on" in control.handle("/status")
    assert control.handle("/unknown") == "Unsupported command."


def test_close_all_uses_executor_and_stays_stopped_after_success(tmp_path) -> None:
    broker, store, _, executor, control = _broker_control(tmp_path)
    _open_position(executor)

    assert control.handle("/close_all") == "Reply YES within 30 seconds to confirm."
    reply = control.handle("YES")

    assert reply == "All bot positions closed; new entries remain stopped."
    assert broker.positions(712345) == ()
    assert store.open_trades() == ()
    assert store.get_flag("stop_new")
    assert not store.get_flag("close_all_pending")


def test_close_all_keeps_pending_after_market_failure_and_retries(tmp_path) -> None:
    broker, store, _, executor, control = _broker_control(tmp_path)
    _open_position(executor)
    broker.tick_data.pop("EURUSD")

    control.handle("/close_all")
    reply = control.handle("YES")

    assert "remains pending" in reply
    assert store.get_flag("close_all_pending")
    assert broker.positions(712345)

    broker.tick_data["EURUSD"] = Tick(NOW, 1.1, 1.1001)
    assert control.retry_close_all() == "All bot positions closed; new entries remain stopped."
    assert broker.positions(712345) == ()
    assert not store.get_flag("close_all_pending")


def test_close_all_racing_entry_stops_send_before_broker_order(
    tmp_path, monkeypatch
) -> None:
    broker, store, gateway, executor, control = _broker_control(
        tmp_path, lock_timeout_s=0.5
    )
    monkeypatch.setattr("bot.app.control._CLOSE_ALL_LOCK_TIMEOUT_S", 0.02)
    broker_locked = threading.Event()
    entry_started = threading.Event()
    release = threading.Event()
    entry_results = []
    failures: list[TimeoutError] = []

    def hold_gateway() -> None:
        try:
            gateway.run_serialized(
                lambda _: (
                    broker_locked.set(),
                    release.wait(2),
                )
            )
        except TimeoutError as error:
            failures.append(error)

    def start_entry() -> None:
        entry_started.set()
        entry_results.append(
            executor.open_trade(
                intent_key="entry-racing-close-all",
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
        )

    lock_thread = threading.Thread(target=hold_gateway)
    lock_thread.start()
    assert broker_locked.wait(1)
    entry_thread = threading.Thread(target=start_entry)
    entry_thread.start()
    assert entry_started.wait(1)
    control.handle("/close_all")

    reply = control.handle("YES")

    assert "remains pending" in reply
    assert store.get_flag("stop_new")
    assert store.get_flag("close_all_pending")
    release.set()
    lock_thread.join(2)
    entry_thread.join(2)
    assert not lock_thread.is_alive()
    assert not entry_thread.is_alive()
    assert not failures
    assert entry_results[0].trade is None
    assert entry_results[0].reason == "STOP_NEW"
    assert broker.order_calls == 0
    assert broker.positions(712345) == ()

    assert (
        control.retry_close_all()
        == "All bot positions closed; new entries remain stopped."
    )
