from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import Fault, FaultPlan, SimBroker
from bot.adapters.store import StateStore
from bot.app import engine as engine_module
from bot.app.config import load_settings
from bot.app.engine import Engine, EntryContext, evaluate_candle
from bot.app.executor import ExecutionResult, Executor
from bot.app.news import NewsEvent
from bot.core.clock import SimClock
from bot.core.costs import CostEstimate
from bot.core.gates import GateDecision
from bot.core.risk import EntryRiskResult
from bot.core.sizing import SizeResult
from bot.core.timeutil import month_start_utc, trading_day_start_utc
from bot.core.types import (
    Candle,
    Decision,
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
    evaluated_contexts: list[EntryContext] = []
    actual_evaluate_candle = engine_module.evaluate_candle

    def capture_context(context: EntryContext):
        evaluated_contexts.append(context)
        return actual_evaluate_candle(context)

    monkeypatch.setattr(engine_module, "evaluate_candle", capture_context)
    first = engine.run_entry_lane()
    second = engine.run_entry_lane()

    assert len(first) == 2
    assert all(decision.reason is ReasonCode.STOP_NEW for decision in first)
    assert [
        context.rollover_at_utc for context in evaluated_contexts
    ] == [datetime(2026, 10, 26, 21, tzinfo=UTC)] * 2
    assert [
        context.weekend_close_utc for context in evaluated_contexts
    ] == [datetime(2026, 10, 30, 21, 55, tzinfo=UTC)] * 2
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


def test_default_context_factory_reads_broker_and_store_risk_state(tmp_path) -> None:
    base_context = _context()
    settings = base_context.settings.model_copy(
        update={
            "symbols": {"EURUSD": base_context.settings.symbols["EURUSD"]},
            "account": base_context.settings.account.model_copy(
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
    store = StateStore(tmp_path / "context-factory.db")
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
        intent_key="context-open",
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
    pending = store.create_intent(
        intent_key="trade|A|H1|pending",
        symbol="EURUSD",
        side=Side.SHORT.value,
        lots=0.01,
        entry=1.1,
        stop_loss=1.101,
        take_profit=1.098,
        actual_risk=2.0,
        created_at_utc=NOW.isoformat(),
        code_version="code",
        settings_version="settings",
    )
    store.transition_intent(pending.intent_id, "SENT")
    day_start = trading_day_start_utc(NOW, broker.server_offset_hours)
    store.new_baseline("day", day_start.isoformat(), 3_000)
    store.new_baseline(
        "month",
        month_start_utc(NOW, broker.server_offset_hours).isoformat(),
        3_000,
    )
    store.set_flag("stop_new", True)
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        code_version="code",
        settings_version="settings",
    )

    context = engine._build_entry_context(
        "EURUSD",
        "H1",
        base_context.candles,
    )

    assert context.equity == broker.account().equity
    assert context.specs == {"EURUSD": SPEC}
    assert context.tick == broker.tick_data["EURUSD"]
    assert [risk.risk_pct for risk in context.open_risks] == pytest.approx(
        [opened.trade.actual_risk / context.equity * 100]
    )
    assert [risk.risk_pct for risk in context.pending_risks] == pytest.approx(
        [2 / context.equity * 100]
    )
    assert context.start_of_day_equity == 3_000
    assert context.realized_pnl_today == 0
    expected_margin = (
        SPEC.contract_size
        * 0.01
        * (1.1001 + 1.1)
        / broker.account_snapshot.leverage
    )
    assert context.margin_used_after_pct == pytest.approx(
        expected_margin / context.equity * 100
    )
    assert context.stop_new


def test_candidate_margin_is_included_before_the_final_risk_decision(
    tmp_path, monkeypatch
) -> None:
    context = _context(margin_used_after_pct=29.9)
    settings = context.settings.model_copy(
        update={
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            )
        }
    )
    context = replace(context, settings=settings)
    clock = SimClock(NOW)
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
    )
    store = StateStore(tmp_path / "candidate-margin.db")
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
        lambda symbol, timeframe, candles: context,
        code_version="code",
        settings_version="settings",
    )
    proposal = Proposal(Side.LONG, 1.1001, 1.099, 1.102, "test")
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda _: ("A", StrategyResult(proposal, None)),
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

    assert evaluate_candle(context).reason is ReasonCode.ENTRY_ALLOWED
    with_margin = engine._with_candidate_margin(
        context,
        proposal,
        settings.symbols["EURUSD"].commission_per_lot_side,
    )

    assert with_margin.margin_used_after_pct > settings.risk.max_margin_used_pct
    assert evaluate_candle(with_margin).reason is ReasonCode.NO_MARGIN
    monkeypatch.setattr(gateway, "calculate_margin", lambda request: None)
    unavailable_margin = engine._with_candidate_margin(
        context,
        proposal,
        settings.symbols["EURUSD"].commission_per_lot_side,
    )
    assert unavailable_margin.margin_calculation_failed
    assert evaluate_candle(unavailable_margin).reason is ReasonCode.NO_MARGIN


def test_unexpected_entry_failure_halts_entries_but_keeps_management_available(
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
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "entry-error.db")
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
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )
    offset = broker.server_utc_offset_hours()
    store.new_baseline("day", trading_day_start_utc(NOW, offset).isoformat(), 3_000)
    store.new_baseline("month", month_start_utc(NOW, offset).isoformat(), 3_000)

    def fail_candles(self, symbol: str, timeframe: str):
        raise RuntimeError(f"simulated {symbol} {timeframe} failure")

    monkeypatch.setattr(SimBroker, "candles", fail_candles)

    with pytest.raises(RuntimeError, match="simulated EURUSD H1 failure"):
        engine.run_entry_lane()

    assert store.get_flag("halted_error")
    assert any(
        alert.severity == "CRITICAL" and "Entry lane failed" in alert.message
        for alert in store.pending_alerts()
    )
    assert not engine.run_management_lane().degraded
    assert store.get_meta("heartbeat_utc") == NOW.isoformat()


def test_entry_lane_carries_accepted_trade_risk_to_later_contexts(
    tmp_path, monkeypatch
) -> None:
    context = _context()
    symbol_names = ("EURUSD", "GBPUSD", "AUDUSD")
    symbol_settings = {
        symbol: context.settings.symbols[symbol].model_copy(
            update={
                "hours_utc": ("mon 00:00-21:55", "fri 00:00-21:55"),
                "max_spread_points": 100.0,
            }
        )
        for symbol in symbol_names
    }
    settings = context.settings.model_copy(
        update={
            "symbols": symbol_settings,
            "account": context.settings.account.model_copy(
                update={"magic_number": 712345}
            ),
        }
    )
    gbp_spec = SymbolSpec(
        "GBPUSD",
        "GBP",
        "USD",
        0.00001,
        0.0001,
        10,
        0.01,
        0.01,
        100,
        contract_size=100_000,
    )
    aud_spec = SymbolSpec(
        "AUDUSD",
        "AUD",
        "USD",
        0.00001,
        0.0001,
        10,
        0.01,
        0.01,
        100,
        contract_size=100_000,
    )
    specs = {"EURUSD": SPEC, "GBPUSD": gbp_spec, "AUDUSD": aud_spec}
    clock = SimClock(NOW)
    candles = {
        (symbol, timeframe): (
            Candle(
                NOW - timedelta(hours=2 if timeframe == "H1" else 8),
                1.1,
                1.1002,
                1.0998,
                1.1001,
            ),
        )
        for symbol in specs
        for timeframe in ("H1", "H4")
    }
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        specs,
        {
            "EURUSD": Tick(NOW, 1.1, 1.1001),
            "GBPUSD": Tick(NOW, 1.1, 1.1001),
            "AUDUSD": Tick(NOW, 1.1, 1.1001),
        },
        clock=clock,
        candle_data=candles,
    )
    store = StateStore(tmp_path / "entry-risk.db")
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
    proposal = Proposal(Side.LONG, 1.1001, 1.099, 1.102, "test")
    monkeypatch.setattr(
        engine_module,
        "_gate",
        lambda *args, **kwargs: GateDecision(True),
    )
    monkeypatch.setattr(
        engine_module,
        "_propose",
        lambda context: (
            "A",
            StrategyResult(
                Proposal(Side.SHORT, 1.1, 1.102, 1.098, "test")
                if context.symbol == "GBPUSD"
                else proposal,
                None,
            ),
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "_size",
        lambda *args, **kwargs: SizeResult(
            0.01,
            15.0,
            None,
            CostEstimate(1.0, 1.0, 1.0),
        ),
    )
    engine = Engine(
        settings,
        gateway,
        store,
        executor,
        clock,
        lambda symbol, timeframe, window: _context(
            settings=settings,
            symbol=symbol,
            timeframe=timeframe,
            candles=window,
            spec=specs[symbol],
            specs=specs,
        ),
        code_version="code",
        settings_version="settings",
    )

    decisions = engine.run_entry_lane()

    assert [decision.reason for decision in decisions] == [
        ReasonCode.ENTRY_ALLOWED,
        ReasonCode.SLOT_TAKEN,
        ReasonCode.ENTRY_ALLOWED,
        ReasonCode.SLOT_TAKEN,
        ReasonCode.DAILY_HEADROOM,
        ReasonCode.DAILY_HEADROOM,
    ]
    assert len(broker.positions(712345)) == 2


@pytest.mark.parametrize(
    ("status", "execution_reason", "expected_reason"),
    [
        ("FAILED", "INVALID_STOPS", ReasonCode.SEND_FAILED),
        ("FAILED", "UNKNOWN_OUTCOME", ReasonCode.SEND_FAILED),
        ("FAILED", "STOP_NEW", ReasonCode.STOP_NEW),
        ("FAILED", "CHECK_FAILED", ReasonCode.CHECK_FAILED),
        ("UNKNOWN", "UNKNOWN_OUTCOME", ReasonCode.UNKNOWN_OUTCOME),
        ("SENT", "UNKNOWN_OUTCOME", ReasonCode.UNKNOWN_OUTCOME),
    ],
)
def test_entry_lane_preserves_execution_failure_reasons(
    tmp_path, monkeypatch, status, execution_reason, expected_reason
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
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        candle_data={
            ("EURUSD", "H1"): (
                Candle(NOW - timedelta(hours=1), 1.1, 1.1002, 1.0998, 1.1001),
            )
        },
    )
    store = StateStore(tmp_path / "entry-result.db")
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
    proposal = Proposal(Side.LONG, 1.1001, 1.099, 1.102, "test")
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
    intent = store.create_intent(
        intent_key=f"test-{status}-{execution_reason}",
        symbol="EURUSD",
        side=Side.LONG.value,
        lots=0.01,
        entry=1.1001,
        stop_loss=1.099,
        take_profit=1.102,
        actual_risk=1.0,
        created_at_utc=NOW.isoformat(),
        code_version="code",
        settings_version="settings",
    )
    store.transition_intent(intent.intent_id, status)
    execution = ExecutionResult(
        store.get_intent(intent.intent_id),
        None,
        execution_reason,
    )
    monkeypatch.setattr(
        executor,
        "open_trade",
        lambda **kwargs: execution,
    )
    monkeypatch.setattr(
        engine_module,
        "evaluate_candle",
        lambda context: Decision(
            context.symbol,
            context.now_utc,
            ReasonCode.ENTRY_ALLOWED,
            strategy="A",
            proposal=proposal,
        ),
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

    decisions = engine.run_entry_lane()

    assert len(decisions) == 1
    assert decisions[0].reason is expected_reason


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


def test_management_lane_closes_strategy_b_after_24_closed_h1_candles(
    tmp_path,
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
    bars = tuple(
        Candle(
            NOW + timedelta(hours=index),
            1.1,
            1.1002,
            1.0998,
            1.1001,
        )
        for index in range(1, 25)
    )
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        candle_data={("EURUSD", "H1"): bars},
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "strategy-b-time-stop.db")
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
        intent_key="strategy-b-time-stop",
        symbol="EURUSD",
        side=Side.LONG,
        lots=0.01,
        entry=1.1001,
        stop_loss=1.099,
        take_profit=1.102,
        actual_risk=1.1,
        strategy="B",
        timeframe="H1",
    )
    assert opened.trade is not None
    clock.advance(timedelta(hours=25))
    broker.tick_data["EURUSD"] = Tick(clock.now_utc(), 1.1, 1.1001)
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

    assert f"time-stop:{opened.trade.ticket}" in report.actions
    assert broker.positions(712345) == ()
    closed = store._connection().execute(
        "SELECT close_reason FROM trades WHERE ticket = ?", (opened.trade.ticket,)
    ).fetchone()
    assert closed["close_reason"] == ExitReason.TIME_STOP.value


def test_management_lane_applies_close_all_without_clearing_stop_new_and_records_by_minute(
    tmp_path,
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
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        session_hours={"EURUSD": ("mon 00:00-21:55", "fri 00:00-21:55")},
    )
    store = StateStore(tmp_path / "close-all.db")
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
        intent_key="close-all-test",
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
    store.set_flag("stop_new", True)
    store.set_flag("close_all_pending", True)
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
    engine.run_management_lane()

    assert f"close-all:{opened.trade.ticket}" in report.actions
    assert broker.positions(712345) == ()
    assert not store.get_flag("close_all_pending")
    assert store.get_flag("stop_new")
    assert store._connection().execute(
        "SELECT COUNT(*) AS count FROM equity_log"
    ).fetchone()["count"] == 1


def test_management_gateway_failure_blocks_entries_and_recovers_on_retry(
    tmp_path,
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
    broker = SimBroker(
        BrokerAccount(1, "Demo", "USD", 3_000, 3_000, True, True, "hedging"),
        {"EURUSD": SPEC},
        {"EURUSD": Tick(NOW, 1.1, 1.1001)},
        clock=clock,
        fault_plan=FaultPlan((Fault(1, "stale_tick", operation="tick"),)),
    )
    store = StateStore(tmp_path / "degraded-management.db")
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
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )

    failed = engine.run_management_lane()

    assert failed.degraded
    assert store.get_meta("gateway_degraded") == "1"
    assert engine.run_entry_lane() == ()

    recovered = engine.run_management_lane()

    assert not recovered.degraded
    assert store.get_meta("gateway_degraded") == "0"
    assert sum(
        alert.message == "Broker gateway is degraded"
        for alert in store.pending_alerts()
    ) == 1


def test_management_lane_rolls_day_and_month_baselines_at_server_boundaries(
    tmp_path,
) -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    context = _context(now_utc=now)
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": context.settings.symbols["EURUSD"]},
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
        server_offset_hours=3,
    )
    store = StateStore(tmp_path / "baseline-rollover.db")
    previous_day = trading_day_start_utc(now - timedelta(days=1), 3)
    previous_month = month_start_utc(now - timedelta(days=1), 3)
    store.new_baseline("day", previous_day.isoformat(), 3_000)
    store.new_baseline("month", previous_month.isoformat(), 3_000)
    store.set_flag("halted_daily", True)
    store.set_flag("halted_monthly", True)
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
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings",
    )

    engine.run_management_lane()

    assert store.get_baseline("day") == (trading_day_start_utc(now, 3).isoformat(), 3_000)
    assert store.get_baseline("month") == (month_start_utc(now, 3).isoformat(), 3_000)
    assert not store.get_flag("halted_daily")
    assert not store.get_flag("halted_monthly")


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
    scheduled_symbol = context.settings.symbols["EURUSD"].model_copy(
        update={
            "hours_utc": (
                "mon 00:00-21:55",
                "tue 00:00-21:55",
                "wed 00:00-21:55",
                "thu 00:00-21:55",
                "fri 00:00-21:55",
            )
        }
    )
    settings = context.settings.model_copy(
        update={
            "symbols": {"EURUSD": scheduled_symbol},
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
        lambda symbol, timeframe, candles: replace(
            _context(
                settings=settings,
                symbol=symbol,
                timeframe=timeframe,
                candles=candles,
                tick=broker.tick_data[symbol],
            ),
            settings=settings,
        ),
        code_version="code",
        settings_version="settings-hash",
    )

    report = engine.start()

    assert not report.degraded
    assert store.get_meta("settings_version") == "settings-hash"
    assert store.get_baseline("day") is not None
    assert store.get_baseline("month") is not None
    assert store.get_meta("baseline_late_utc") == NOW.isoformat()
    assert store.get_meta("baseline_late_periods") == "day,month"
    assert any(
        "baseline_late" in alert.message
        and "limits are measured from current equity" in alert.message
        for alert in store.pending_alerts()
    )
    assert store.get_meta("heartbeat_utc") == NOW.isoformat()
    assert store.get_meta("news_calendar_mode") == "enabled"
    assert store.get_meta("news_calendar_valid") == "1"
    assert any(
        "coverage is under 10 days" in alert.message
        for alert in store.pending_alerts()
    )

    opened = executor.open_trade(
        intent_key="restart-midday-position",
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

    clock.advance(timedelta(days=3))
    broker.tick_data["EURUSD"] = Tick(clock.now_utc(), 1.1, 1.1001)
    decisions = engine.run_entry_lane()
    assert len(decisions) == 2
    assert [decision.reason for decision in decisions] == [
        ReasonCode.NEWS_CALENDAR_STALE,
        ReasonCode.NEWS_CALENDAR_STALE,
    ]

    news_path.unlink()
    engine._initialize_news_calendar()
    engine._initialize_news_calendar()

    assert store.get_meta("news_calendar_valid") == "0"
    assert sum(
        "News calendar is missing" in alert.message
        for alert in store.pending_alerts()
    ) == 1

    decision_count = store._connection().execute(
        "SELECT COUNT(*) AS count FROM decisions"
    ).fetchone()["count"]
    engine.stop()

    restarted_store = StateStore(tmp_path / "startup.db")
    restarted_gateway = BrokerGateway(broker, max_retries=0)
    restarted_executor = Executor(
        restarted_gateway,
        restarted_store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings-hash",
        wait=lambda _: None,
    )
    restarted_engine = Engine(
        settings,
        restarted_gateway,
        restarted_store,
        restarted_executor,
        clock,
        lambda symbol, timeframe, candles: _context(
            settings=settings, symbol=symbol, timeframe=timeframe, candles=candles
        ),
        code_version="code",
        settings_version="settings-hash",
    )

    restarted_engine.start()

    assert len(broker.positions(712345)) == 1
    assert broker.positions(712345)[0].stop_loss > 0
    restarted_day_baseline = restarted_store.get_baseline("day")
    assert restarted_day_baseline is not None
    assert restarted_day_baseline[0] == trading_day_start_utc(
        clock.now_utc(), broker.server_offset_hours
    ).isoformat()
    assert restarted_day_baseline[1] == pytest.approx(broker.account().equity)
    assert restarted_store.get_meta("baseline_late_periods") == "day"
    assert [trade.ticket for trade in restarted_store.open_trades()] == [
        opened.trade.ticket
    ]
    assert restarted_engine.run_entry_lane() == ()
    assert (
        restarted_store._connection()
        .execute("SELECT COUNT(*) AS count FROM decisions")
        .fetchone()["count"]
        == decision_count
    )
    restarted_engine.stop()


def test_engine_simulated_week_covers_gates_limits_flatten_and_restart(
    tmp_path,
) -> None:
    news_path = tmp_path / "week-news.csv"
    news_path.write_text(
        "time_utc,currency,impact,title\n"
        f"{(NOW + timedelta(days=1)).isoformat()},USD,high,Fake event one\n"
        f"{(NOW + timedelta(days=5)).isoformat()},EUR,high,Fake event two\n"
        f"{(NOW + timedelta(days=9)).isoformat()},GBP,high,Fake event three\n",
        encoding="utf-8",
    )
    base_context = _context()
    symbol_settings = base_context.settings.symbols["EURUSD"].model_copy(
        update={
            "hours_utc": (
                "mon 00:00-21:55",
                "tue 00:00-21:55",
                "wed 00:00-21:55",
                "thu 00:00-21:55",
                "fri 00:00-21:55",
            )
        }
    )
    settings = base_context.settings.model_copy(
        update={
            "symbols": {"EURUSD": symbol_settings},
            "account": base_context.settings.account.model_copy(
                update={
                    "expected_login": 1,
                    "expected_server": "Demo",
                    "magic_number": 712345,
                }
            ),
            "news": base_context.settings.news.model_copy(
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
        server_offset_hours=3,
        session_hours={
            "EURUSD": (
                "mon 00:00-21:55",
                "tue 00:00-21:55",
                "wed 00:00-21:55",
                "thu 00:00-21:55",
                "fri 00:00-21:55",
            )
        },
        candle_data={
            ("EURUSD", "H1"): (
                Candle(NOW - timedelta(hours=1), 1.1, 1.1002, 1.0998, 1.1001),
            ),
            ("EURUSD", "H4"): (
                Candle(NOW - timedelta(hours=4), 1.1, 1.1002, 1.0998, 1.1001),
            ),
        },
    )
    database = tmp_path / "simulated-week.db"
    store = StateStore(database)
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

    def make_engine(
        active_store: StateStore,
        active_gateway: BrokerGateway,
        active_executor: Executor,
    ) -> Engine:
        return Engine(
            settings,
            active_gateway,
            active_store,
            active_executor,
            clock,
            code_version="code",
            settings_version="settings-hash",
        )

    def set_market(at: datetime, h1_open: datetime, h4_open: datetime) -> None:
        clock.set(at)
        broker.tick_data["EURUSD"] = Tick(at, 1.1, 1.1001)
        broker.candle_data[("EURUSD", "H1")] = (
            Candle(h1_open, 1.1, 1.1002, 1.0998, 1.1001),
        )
        broker.candle_data[("EURUSD", "H4")] = (
            Candle(h4_open, 1.1, 1.1002, 1.0998, 1.1001),
        )

    engine = make_engine(store, gateway, executor)
    assert not engine.start().degraded
    engine.run_entry_lane()

    rollover_time = NOW.replace(hour=21)
    set_market(
        rollover_time,
        rollover_time - timedelta(hours=1),
        rollover_time - timedelta(hours=5),
    )
    rollover_decisions = engine.run_entry_lane()
    assert len(rollover_decisions) == 2
    assert all(
        decision.reason is ReasonCode.ROLLOVER for decision in rollover_decisions
    )
    engine.run_management_lane()

    news_time = NOW + timedelta(days=1)
    set_market(
        news_time,
        news_time - timedelta(hours=1),
        news_time - timedelta(hours=4),
    )
    news_decisions = engine.run_entry_lane()
    assert len(news_decisions) == 2
    assert all(decision.reason is ReasonCode.NEWS_WINDOW for decision in news_decisions)

    open_time = news_time + timedelta(hours=2)
    set_market(open_time, open_time - timedelta(hours=1), news_time - timedelta(hours=4))
    engine.run_entry_lane()
    opened = executor.open_trade(
        intent_key="simulated-week-daily-limit",
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
    broker._balance = 2_960
    daily_report = engine.run_management_lane()
    assert "daily-halt" in daily_report.actions
    assert store.get_flag("halted_daily")
    assert len(broker.positions(712345)) == 1
    assert broker.positions(712345)[0].stop_loss > 0
    decision_count = store._connection().execute(
        "SELECT COUNT(*) AS count FROM decisions"
    ).fetchone()["count"]
    engine.stop()

    restarted_store = StateStore(database)
    restarted_gateway = BrokerGateway(broker, max_retries=0)
    restarted_executor = Executor(
        restarted_gateway,
        restarted_store,
        clock,
        magic_number=712345,
        max_send_retries=0,
        unknown_outcome_wait_s=1,
        code_version="code",
        settings_version="settings-hash",
        wait=lambda _: None,
    )
    restarted_engine = make_engine(
        restarted_store, restarted_gateway, restarted_executor
    )
    assert not restarted_engine.start().degraded
    assert restarted_engine.run_entry_lane() == ()
    assert restarted_store.get_flag("halted_daily")
    assert restarted_store._connection().execute(
        "SELECT COUNT(*) AS count FROM decisions"
    ).fetchone()["count"] == decision_count

    monthly_time = NOW + timedelta(days=2)
    set_market(monthly_time, monthly_time - timedelta(hours=1), monthly_time - timedelta(hours=4))
    broker._balance = 2_890
    monthly_report = restarted_engine.run_management_lane()
    assert "monthly-halt" in monthly_report.actions
    assert f"monthly-close:{opened.trade.ticket}" in monthly_report.actions
    assert broker.positions(712345) == ()

    next_month_time = datetime(2026, 11, 2, 12, tzinfo=UTC)
    set_market(
        next_month_time,
        next_month_time - timedelta(hours=1),
        next_month_time - timedelta(hours=4),
    )
    restarted_engine.run_management_lane()
    assert not restarted_store.get_flag("halted_daily")
    assert not restarted_store.get_flag("halted_monthly")

    friday_time = datetime(2026, 11, 6, 21, 40, tzinfo=UTC)
    set_market(
        friday_time,
        friday_time - timedelta(hours=1),
        friday_time - timedelta(hours=4),
    )
    friday_trade = restarted_executor.open_trade(
        intent_key="simulated-week-friday-flatten",
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
    assert friday_trade.trade is not None
    friday_report = restarted_engine.run_management_lane()
    assert f"weekend-close:{friday_trade.trade.ticket}" in friday_report.actions
    assert broker.positions(712345) == ()
    assert restarted_store.open_trades() == ()
    restarted_engine.stop()
