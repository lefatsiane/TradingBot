from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import SimBroker
from bot.adapters.store import StateStore
from bot.app.config import load_settings
from bot.app.engine import Engine
from bot.app.executor import Executor
from bot.app.runtime import (
    BotRuntime,
    CrashLoopGuard,
    RuntimeFileLock,
    _block_new_entries_for_clock_mismatch,
)
from bot.core.clock import SimClock
from bot.core.types import Side, SymbolSpec

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


def test_server_clock_mismatch_blocks_entries_and_queues_critical_alert(
    tmp_path,
) -> None:
    store = StateStore(tmp_path / "runtime.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))

    _block_new_entries_for_clock_mismatch(store, clock, "offset mismatch")

    assert store.get_flag("stop_new")
    alerts = store.pending_alerts()
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"
    assert "offset mismatch" in alerts[0].message


def test_third_unplanned_start_sets_stop_new_and_alerts(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    guard = CrashLoopGuard(store, clock)

    assert not guard.record_start(planned=False)
    clock.advance(timedelta(minutes=1))
    assert not guard.record_start(planned=False)
    clock.advance(timedelta(minutes=1))

    assert guard.record_start(planned=False)
    assert store.get_flag("stop_new")
    alerts = store.pending_alerts()
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"


def test_old_and_planned_starts_do_not_count_toward_crash_loop(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime.db")
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    guard = CrashLoopGuard(store, clock)

    assert not guard.record_start(planned=False)
    clock.advance(timedelta(hours=2))
    assert not guard.record_start(planned=True)
    clock.advance(timedelta(seconds=1))
    assert not guard.record_start(planned=False)


def test_runtime_file_lock_refuses_a_second_instance(tmp_path) -> None:
    first = RuntimeFileLock(tmp_path / "runtime" / "bot.lock")
    second = RuntimeFileLock(tmp_path / "runtime" / "bot.lock")
    first.acquire()

    try:
        with pytest.raises(RuntimeError, match="Another runtime instance"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runtime_clean_shutdown_leaves_protected_positions_open(tmp_path) -> None:
    settings_path = Path(__file__).parents[1] / "settings.toml"
    settings, version = load_settings(settings_path)
    symbols = {
        name: configured.model_copy(update={"enabled": False})
        for name, configured in settings.symbols.items()
    }
    symbols["EURUSD"] = symbols["EURUSD"].model_copy(
        update={
            "enabled": True,
            "hours_utc": ("mon 00:00-21:55", "fri 00:00-21:55"),
            "hours_utc_season": "US summer",
        }
    )
    settings = settings.model_copy(
        update={
            "symbols": symbols,
            "account": settings.account.model_copy(
                update={
                    "expected_login": 1,
                    "expected_server": "Demo",
                    "magic_number": 712345,
                }
            ),
        }
    )
    clock = SimClock(datetime(2026, 10, 26, 12, tzinfo=UTC))
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(clock.now_utc(), 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": settings.symbols["EURUSD"].hours_utc},
    )
    store = StateStore(tmp_path / "runtime.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="test",
        settings_version=version,
        wait=lambda _: None,
    )
    opened = executor.open_trade(
        intent_key="runtime-preserved-position",
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
    assert opened.trade is not None
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        code_version="test",
        settings_version=version,
        news_enabled=False,
    )
    shutdown_calls: list[bool] = []
    startup_reports: list[bool] = []
    runtime = BotRuntime(
        engine,
        store,
        clock,
        fast_tick_s=1,
        candle_poll_s=1,
        logger=logging.getLogger("test.runtime"),
        startup_report=lambda: startup_reports.append(True),
        broker_shutdown=lambda: shutdown_calls.append(True),
    )
    stop_event = threading.Event()
    stop_event.set()

    runtime.run(stop_event)

    assert broker.positions(712345)[0].ticket == opened.trade.ticket
    assert broker.positions(712345)[0].stop_loss > 0
    assert store.open_trades()[0].ticket == opened.trade.ticket
    assert store.get_meta("clean_shutdown") == "1"
    assert startup_reports == [True]
    assert shutdown_calls == [True]
