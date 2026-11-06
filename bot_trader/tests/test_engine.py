from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import SimBroker
from bot.adapters.store import StateStore
from bot.app import engine as engine_module
from bot.app.config import load_settings
from bot.app.engine import Engine, EntryContext, evaluate_candle
from bot.app.executor import Executor
from bot.app.news import NewsEvent
from bot.core.clock import SimClock
from bot.core.costs import CostEstimate
from bot.core.gates import GateDecision
from bot.core.risk import EntryRiskResult
from bot.core.sizing import SizeResult
from bot.core.timeutil import month_start_utc, trading_day_start_utc
from bot.core.types import (
    Candle,
    ExitReason,
    OpenRisk,
    Proposal,
    ReasonCode,
    Side,
    StrategyResult,
    SymbolSpec,
)

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
SPEC = SymbolSpec(
    "EURUSD", "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
    contract_size=100_000,
)


def _context(**updates) -> EntryContext:
    settings, _ = load_settings(Path(__file__).parents[1] / "settings.toml")
    symbol_settings = settings.symbols["EURUSD"].model_copy(
        update={
            "hours_utc": ("mon 00:00-21:55", "fri 00:00-21:55"),
            "max_spread_points": 100.0,
        }
    )
    settings = settings.model_copy(
        update={
            "symbols": {**settings.symbols, "EURUSD": symbol_settings},
        }
    )
    candle = Candle(NOW - timedelta(hours=1), 1.1, 1.1002, 1.0998, 1.1001)
    values = {
        "settings": settings,
        "symbol": "EURUSD",
        "timeframe": "H1",
        "candles": (candle,),
        "now_utc": NOW,
        "spec": SPEC,
        "tick": Tick(NOW, 1.1, 1.1001),
        "specs": {"EURUSD": SPEC},
        "equity": 3_000.0,
        "start_of_day_equity": 3_000.0,
        "rollover_at_utc": NOW - timedelta(hours=1),
        "weekend_close_utc": NOW + timedelta(days=4),
    }
    values.update(updates)
    return EntryContext(**values)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"stop_new": True, "halted_daily": True}, ReasonCode.STOP_NEW),
        ({"halted_daily": True, "halted_monthly": True}, ReasonCode.DAILY_HALT),
        ({"halted_monthly": True, "halted_error": True}, ReasonCode.MONTHLY_HALT),
        ({"halted_error": True}, ReasonCode.ERROR_HALT),
    ],
)
def test_entry_guardrails_follow_priority_order(flags, expected) -> None:
    assert evaluate_candle(_context(**flags)).reason is expected


def test_stop_new_precedes_stale_tick_data() -> None:
    stale_tick = Tick(NOW - timedelta(minutes=11), 1.1, 1.1001)

    assert (
        evaluate_candle(_context(stop_new=True, tick=stale_tick)).reason
        is ReasonCode.STOP_NEW
    )


def test_weekend_gate_precedes_data_error() -> None:
    context = _context(
        weekend_close_utc=NOW + timedelta(minutes=60),
        data_error=True,
    )

    assert evaluate_candle(context).reason is ReasonCode.WEEKEND_WINDOW


def test_data_error_precedes_disabled_symbol() -> None:
    context = _context(data_error=True)
    settings = context.settings
    disabled = settings.symbols["EURUSD"].model_copy(update={"enabled": False})
    context = replace(
        context,
        settings=settings.model_copy(
            update={"symbols": {**settings.symbols, "EURUSD": disabled}}
        ),
    )

    assert evaluate_candle(context).reason is ReasonCode.DATA_ERROR


def test_disabled_symbol_and_outside_hours_are_blocked() -> None:
    context = _context()
    disabled = context.settings.symbols["EURUSD"].model_copy(
        update={"enabled": False}
    )
    context = replace(
        context,
        settings=context.settings.model_copy(
            update={"symbols": {**context.settings.symbols, "EURUSD": disabled}}
        ),
    )
    assert evaluate_candle(context).reason is ReasonCode.SYMBOL_DISABLED

    outside = _context(
        settings=context.settings.model_copy(
            update={
                "symbols": {
                    **context.settings.symbols,
                    "EURUSD": context.settings.symbols["EURUSD"].model_copy(
                        update={"enabled": True, "hours_utc": ("mon 00:00-11:00",)}
                    ),
                }
            }
        )
    )
    assert evaluate_candle(outside).reason is ReasonCode.OUTSIDE_HOURS


def test_stale_and_blocked_news_calendar_reasons() -> None:
    assert (
        evaluate_candle(_context(news_calendar_valid=False)).reason
        is ReasonCode.NEWS_CALENDAR_STALE
    )
    event = NewsEvent(NOW, "EUR", "high", "fake test event")
    assert (
        evaluate_candle(_context(news_events=(event,))).reason
        is ReasonCode.NEWS_WINDOW
    )


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.WARMUP,
        ReasonCode.NO_TREND,
        ReasonCode.NO_PULLBACK,
        ReasonCode.NO_TRIGGER,
        ReasonCode.STOP_TOO_WIDE,
        ReasonCode.NO_QUIET_MARKET,
        ReasonCode.NO_STRETCH,
        ReasonCode.REWARD_TOO_SMALL,
    ],
)
def test_strategy_skip_reason_is_preserved(monkeypatch, reason) -> None:
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: ("A", StrategyResult(None, reason)),
    )

    assert evaluate_candle(_context()).reason is reason


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"open_risks": (OpenRisk("EURUSD", Side.LONG, 0.2),)},
            ReasonCode.SLOT_TAKEN,
        ),
        ({"bars_since_stop": 0}, ReasonCode.COOLDOWN),
    ],
)
def test_slot_and_cooldown_precede_strategy_skip(
    monkeypatch, updates, expected
) -> None:
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: ("A", StrategyResult(None, ReasonCode.NO_TRIGGER)),
    )

    assert evaluate_candle(_context(**updates)).reason is expected


def test_rollover_precedes_slot_taken() -> None:
    context = _context(
        open_risks=(OpenRisk("EURUSD", Side.LONG, 0.2),),
        rollover_at_utc=NOW,
    )

    assert evaluate_candle(context).reason is ReasonCode.ROLLOVER


@pytest.mark.parametrize(
    ("size_reason", "expected"),
    [
        ("COST_TOO_HIGH", ReasonCode.COST_TOO_HIGH),
        ("BELOW_MIN_LOT", ReasonCode.BELOW_MIN_LOT),
    ],
)
def test_size_reasons_are_preserved(monkeypatch, size_reason, expected) -> None:
    proposal = Proposal(Side.LONG, 1.1, 1.099, 1.102, "test")
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: ("A", StrategyResult(proposal, None)),
    )
    monkeypatch.setattr(
        engine_module,
        "_size",
        lambda *args, **kwargs: SizeResult(
            None,
            None,
            size_reason,
            CostEstimate(1.0, 1.0, 100.0),
        ),
    )

    assert evaluate_candle(_context()).reason is expected


@pytest.mark.parametrize(
    ("risk_reason", "expected"),
    [
        ("SLOT_TAKEN", ReasonCode.SLOT_TAKEN),
        ("COOLDOWN", ReasonCode.COOLDOWN),
        ("NO_MARGIN", ReasonCode.NO_MARGIN),
        ("MAX_TRADES", ReasonCode.MAX_TRADES),
        ("CURRENCY_EXPOSURE", ReasonCode.CURRENCY_EXPOSURE),
        ("DAILY_HEADROOM", ReasonCode.DAILY_HEADROOM),
    ],
)
def test_entry_risk_reason_is_preserved(monkeypatch, risk_reason, expected) -> None:
    proposal = Proposal(Side.LONG, 1.1, 1.099, 1.102, "test")
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: ("A", StrategyResult(proposal, None)),
    )
    monkeypatch.setattr(
        engine_module,
        "_size",
        lambda *args, **kwargs: SizeResult(
            0.01,
            1.0,
            None,
            CostEstimate(100.0, 1.0, 1.0),
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "evaluate_entry",
        lambda **kwargs: EntryRiskResult(False, risk_reason, (), 0.0),
    )

    assert evaluate_candle(_context()).reason is expected


def test_order_check_failure_is_reported_after_risk_checks(monkeypatch) -> None:
    proposal = Proposal(Side.LONG, 1.1, 1.099, 1.102, "test")
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: ("A", StrategyResult(proposal, None)),
    )
    monkeypatch.setattr(
        engine_module,
        "_size",
        lambda *args, **kwargs: SizeResult(
            0.01,
            1.0,
            None,
            CostEstimate(1.0, 1.0, 1.0),
        ),
    )

    assert (
        evaluate_candle(_context(check_passed=False)).reason
        is ReasonCode.CHECK_FAILED
    )


def test_news_event_precedes_rollover_reason() -> None:
    event = NewsEvent(NOW, "EUR", "high", "fake test event")

    assert (
        evaluate_candle(
            _context(news_events=(event,), rollover_at_utc=NOW)
        ).reason
        is ReasonCode.NEWS_WINDOW
    )


def test_rollover_spread_and_volatility_gate_reasons() -> None:
    assert (
        evaluate_candle(_context(rollover_at_utc=NOW)).reason
        is ReasonCode.ROLLOVER
    )
    assert (
        evaluate_candle(_context(tick=Tick(NOW, 1.1, 1.102))).reason
        is ReasonCode.SPREAD_WIDE
    )

    quiet = [
        Candle(
            NOW - timedelta(hours=100 - index),
            1.1,
            1.10001,
            1.09999,
            1.1,
        )
        for index in range(99)
    ]
    quiet.append(Candle(NOW - timedelta(hours=1), 1.1, 1.11, 1.09, 1.1))
    assert (
        evaluate_candle(_context(candles=tuple(quiet))).reason
        is ReasonCode.SKIP_VOLATILITY
    )


def test_strategy_warmup_skip_and_missing_time_inputs_fail_closed() -> None:
    assert evaluate_candle(_context()).reason is ReasonCode.WARMUP
    assert (
        evaluate_candle(_context(rollover_at_utc=None)).reason
        is ReasonCode.DATA_ERROR
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_candle(_context(now_utc=datetime(2026, 10, 26, 12)))


def test_entry_lane_processes_closed_bars_once_in_settings_order(
    tmp_path, monkeypatch
) -> None:
    context = _context()
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": context.settings.symbols["EURUSD"]},
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            ),
        }
    )
    clock = SimClock(NOW)
    h1 = (
        Candle(NOW - timedelta(hours=2), 1.1, 1.1002, 1.0998, 1.1001),
        Candle(NOW - timedelta(hours=1), 1.1, 1.1002, 1.0998, 1.1001),
    )
    h4 = (
        Candle(NOW - timedelta(hours=8), 1.1, 1.1002, 1.0998, 1.1001),
        Candle(NOW - timedelta(hours=4), 1.1, 1.1002, 1.0998, 1.1001),
    )
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        candle_data={("EURUSD", "H1"): h1, ("EURUSD", "H4"): h4},
    )
    store = StateStore(tmp_path / "engine.db")
    stopped = store.create_intent(
        intent_key="prior-stop",
        symbol="EURUSD",
        side=Side.LONG.value,
        lots=0.01,
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
        actual_risk=1.0,
        created_at_utc=(NOW - timedelta(hours=4)).isoformat(),
        code_version="code",
        settings_version="settings",
    )
    store.transition_intent(stopped.intent_id, "SENT")
    store.transition_intent(stopped.intent_id, "CONFIRMED", ticket="prior")
    store.create_trade_from_intent(
        intent_id=stopped.intent_id,
        ticket="prior",
        strategy="A",
        timeframe="H1",
        opened_at_utc=(NOW - timedelta(hours=4)).isoformat(),
    )
    store.close_trade(
        "prior",
        (NOW - timedelta(minutes=90)).isoformat(),
        1.099,
        -1.0,
        0.0,
        ExitReason.STOP_LOSS,
    )
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings",
        wait=lambda _: None,
    )
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            stop_new=True,
        ),
        code_version="code",
        settings_version="settings",
    )

    assert engine._bars_since_stop("EURUSD", "H1", h1) == 1
    assert engine._bars_since_stop("EURUSD", "H4", h4) is None
    first = engine.run_entry_lane()
    second = engine.run_entry_lane()

    assert len(first) == 2
    assert all(decision.reason is ReasonCode.STOP_NEW for decision in first)
    assert second == ()
    progress = store._connection().execute(
        "SELECT timeframe FROM bar_progress ORDER BY timeframe"
    ).fetchall()
    assert [row["timeframe"] for row in progress] == ["H1", "H4"]

    def missing_candles(*args, **kwargs):
        raise engine_module.DataQualityError("no valid closed candles")

    monkeypatch.setattr(
        engine_module,
        "last_closed_candles",
        missing_candles,
    )
    assert engine.run_entry_lane() == ()
    assert engine.run_entry_lane() == ()
    assert not store.get_flag("halted_error")
    assert len(store.pending_alerts()) == 2
    engine._acquire_instance_lock()
    duplicate = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )
    with pytest.raises(RuntimeError, match="Another bot instance"):
        duplicate._acquire_instance_lock()
    engine.stop()


def test_monthly_loss_limit_flattens_positions_in_management_lane(tmp_path) -> None:
    context = _context()
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": context.settings.symbols["EURUSD"]},
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            ),
        }
    )
    clock = SimClock(NOW)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "management.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings",
        wait=lambda _: None,
    )
    opened = executor.open_trade(
        intent_key="management-test",
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
    offset = broker.server_utc_offset_hours()
    store.new_baseline("day", trading_day_start_utc(NOW, offset).isoformat(), 3_000)
    store.new_baseline("month", month_start_utc(NOW, offset).isoformat(), 3_000)
    broker._balance = 2_900
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )

    report = engine.run_management_lane()

    assert "monthly-halt" in report.actions
    assert f"monthly-close:{opened.trade.ticket}" in report.actions
    assert broker.positions(712345) == ()
    closed = store.open_trades()
    assert closed == ()
    stored = store._connection().execute(
        "SELECT close_reason FROM trades WHERE ticket = ?", (opened.trade.ticket,)
    ).fetchone()
    assert stored["close_reason"] == "MONTHLY_CAP"
    assert store.get_flag("halted_daily")
    assert store.get_flag("halted_monthly")


def test_daily_loss_limit_keeps_protective_stop_and_position_open(tmp_path) -> None:
    context = _context()
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": context.settings.symbols["EURUSD"]},
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            ),
        }
    )
    clock = SimClock(NOW)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "daily.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings",
        wait=lambda _: None,
    )
    opened = executor.open_trade(
        intent_key="daily-halt",
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
    offset = broker.server_utc_offset_hours()
    store.new_baseline("day", trading_day_start_utc(NOW, offset).isoformat(), 3_000)
    store.new_baseline("month", month_start_utc(NOW, offset).isoformat(), 3_000)
    broker._balance = 2_967
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )

    report = engine.run_management_lane()

    assert "daily-halt" in report.actions
    assert "monthly-halt" not in report.actions
    positions = broker.positions(712345)
    assert len(positions) == 1
    assert positions[0].stop_loss > 0
    assert store.get_flag("halted_daily")
    assert not store.get_flag("halted_monthly")


def test_management_lane_flattens_before_configured_friday_close(tmp_path) -> None:
    now = datetime(2026, 10, 30, 21, 40, tzinfo=UTC)
    context = _context(now_utc=now)
    symbol_settings = context.settings.symbols["EURUSD"].model_copy(
        update={"hours_utc": ("mon 00:00-21:55", "fri 00:00-21:55")}
    )
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": symbol_settings},
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            ),
        }
    )
    clock = SimClock(now)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(now, 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "friday.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings",
        wait=lambda _: None,
    )
    opened = executor.open_trade(
        intent_key="friday-flatten",
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
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )

    report = engine.run_management_lane()

    assert f"weekend-close:{opened.trade.ticket}" in report.actions
    assert broker.positions(712345) == ()
    closed = store._connection().execute(
        "SELECT close_reason FROM trades WHERE ticket = ?", (opened.trade.ticket,)
    ).fetchone()
    assert closed["close_reason"] == ExitReason.WEEKEND_FLATTEN.value


def test_engine_start_runs_guards_recovery_and_one_management_pass(tmp_path) -> None:
    context = _context()
    news_path = tmp_path / "news.csv"
    news_path.write_text(
        "time_utc,currency,impact,title\n"
        f"{(NOW + timedelta(days=1)).isoformat()},USD,high,Fake one\n"
        f"{(NOW + timedelta(days=5)).isoformat()},EUR,high,Fake two\n"
        f"{(NOW + timedelta(days=9)).isoformat()},GBP,high,Fake three\n",
        encoding="utf-8",
    )
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": context.settings.symbols["EURUSD"]},
            "account": context.settings.account.model_copy(
                update={
                    "expected_login": 1,
                    "expected_server": "Demo",
                    "magic_number": 712345,
                }
            ),
            "news": context.settings.news.model_copy(
                update={"calendar_file": str(news_path)}
            ),
        }
    )
    clock = SimClock(NOW)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        candle_data={
            ("EURUSD", "H1"): (
                Candle(NOW - timedelta(hours=1), 1.1, 1.1002, 1.0998, 1.1001),
            ),
            ("EURUSD", "H4"): (
                Candle(NOW - timedelta(hours=4), 1.1, 1.1002, 1.0998, 1.1001),
            ),
        },
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "startup.db")
    gateway = BrokerGateway(broker, max_retries=0)
    executor = Executor(
        gateway,
        store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings-hash",
        wait=lambda _: None,
    )
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings-hash",
    )

    report = engine.start()

    assert not report.degraded
    assert store.get_meta("settings_version") == "settings-hash"
    assert store.get_baseline("day") is not None
    assert store.get_baseline("month") is not None
    assert store.get_meta("heartbeat_utc") == NOW.isoformat()
    assert store.get_meta("news_calendar_mode") == "enabled"
    assert store.get_meta("news_calendar_valid") == "1"
    assert any(
        "coverage is under 10 days" in alert.message
        for alert in store.pending_alerts()
    )

    clock.advance(timedelta(days=3))
    decisions = engine.run_entry_lane()
    assert len(decisions) == 2
    assert all(
        decision.reason is ReasonCode.NEWS_CALENDAR_STALE
        for decision in decisions
    )

    news_path.unlink()
    engine._initialize_news_calendar()
    engine._initialize_news_calendar()

    assert store.get_meta("news_calendar_valid") == "0"
    assert sum(
        "News calendar is missing" in alert.message
        for alert in store.pending_alerts()
    ) == 1
    engine.stop()
