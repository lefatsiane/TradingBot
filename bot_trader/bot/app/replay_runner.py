from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Literal
from uuid import uuid4

from bot.adapters.broker import BrokerAccount, BrokerDeal, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import SimBroker
from bot.adapters.store import StateStore
from bot.app.backtest import EquityPoint, select_backtest_history
from bot.app.config import (
    Settings,
    build_simulator_costs,
    schedule_validation_failures,
)
from bot.app.data import DataQualityError, Timeframe
from bot.app.engine import Engine
from bot.app.executor import Executor
from bot.core.clock import SimClock
from bot.core.types import Candle, Decision, SymbolSpec

StrategyMode = Literal["a", "b", "both"]
HistoryMap = Mapping[tuple[str, Timeframe], tuple[Candle, ...]]
SIMULATED_SERVER_OFFSET_HOURS = 3  # DECISION-PENDING: confirm the broker-server offset policy.
DEFAULT_STARTING_EQUITY = 3_000.0


@dataclass(frozen=True, slots=True)
class ReplayRunResult:
    strategy_mode: StrategyMode
    starting_equity: float
    ending_equity: float
    decisions: tuple[Decision, ...]
    deals: tuple[BrokerDeal, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_positions: tuple[dict[str, object], ...]
    excluded_recent_bars: int
    news_enabled: bool
    final_run: bool
    report_dir: Path | None = None


def run_replay(
    histories: HistoryMap,
    specs: Mapping[str, SymbolSpec],
    settings: Settings,
    *,
    now_utc: datetime,
    account: BrokerAccount | None = None,
    starting_equity: float = DEFAULT_STARTING_EQUITY,
    final: bool = False,
    no_news: bool = False,
    strategy_mode: StrategyMode = "both",
    report_root: str | Path | None = None,
) -> ReplayRunResult:
    """Replay cached candles through the production Engine, Executor and SimBroker."""
    now = _require_utc(now_utc, "now_utc")
    if strategy_mode not in ("a", "b", "both"):
        raise ValueError("strategy_mode must be 'a', 'b', or 'both'")
    if settings.account.mode != "demo" or settings.account.allow_live:
        raise ValueError("Replay requires demo-only settings with live trading disabled")
    if starting_equity <= 0 or not isfinite(starting_equity):
        raise ValueError("starting_equity must be positive")

    enabled_symbols = tuple(
        name for name, symbol_settings in settings.symbols.items() if symbol_settings.enabled
    )
    schedule_failures = schedule_validation_failures(settings)
    if schedule_failures:
        raise ValueError(
            "Replay requires reviewed UTC schedules for enabled symbols: "
            + "; ".join(schedule_failures)
        )
    if not enabled_symbols:
        raise ValueError("Replay requires at least one enabled symbol")

    timeframes: tuple[Timeframe, ...] = (
        settings.strategy_a.timeframes
        if strategy_mode == "a"
        else ("H1",)
        if strategy_mode == "b"
        else tuple(dict.fromkeys((*settings.strategy_a.timeframes, "H1")))
    )
    selected_histories: dict[tuple[str, Timeframe], tuple[Candle, ...]] = {}
    excluded_recent_bars = 0
    for symbol in enabled_symbols:
        if symbol not in specs or specs[symbol].name != symbol:
            raise DataQualityError(f"History manifest has no matching specification for {symbol}")
        for timeframe in timeframes:
            key = (symbol, timeframe)
            candles = histories.get(key)
            if candles is None:
                raise DataQualityError(f"Missing cached history for {symbol} {timeframe}")
            selected, excluded = select_backtest_history(
                candles,
                timeframe,
                now_utc=now,
                final=final,
            )
            selected_histories[key] = selected
            excluded_recent_bars += excluded

    first_time = min(candles[0].time for candles in selected_histories.values())
    clock = SimClock(first_time)
    first_h1 = {
        symbol: selected_histories[(symbol, "H1")][0] for symbol in enabled_symbols
    }
    initial_ticks = {
        symbol: _tick_for_candle(candle)
        for symbol, candle in first_h1.items()
    }
    simulated_account = account or BrokerAccount(
        login=settings.account.expected_login,
        server=settings.account.expected_server,
        currency="USD",
        balance=starting_equity,
        equity=starting_equity,
        is_demo=True,
        trade_allowed=True,
        margin_mode="hedging",
    )
    if not simulated_account.is_demo:
        raise ValueError("Replay accepts demo-account snapshots only")
    if account is not None and account.balance != starting_equity:
        starting_equity = account.balance

    broker = SimBroker(
        account_snapshot=simulated_account,
        specs={symbol: specs[symbol] for symbol in enabled_symbols},
        tick_data=initial_ticks,
        clock=clock,
        costs=build_simulator_costs(settings, enabled_symbols),
        session_hours={
            symbol: settings.symbols[symbol].hours_utc for symbol in enabled_symbols
        },
        server_offset_hours=SIMULATED_SERVER_OFFSET_HOURS,
    )
    gateway = BrokerGateway(broker, max_retries=0)
    temp_dir = tempfile.TemporaryDirectory(prefix="tradingbot-replay-")
    temp_root = Path(temp_dir.name)
    store = StateStore(temp_root / "replay.db")
    engine = Engine(
        settings,
        gateway,
        store,
        Executor(
            gateway,
            store,
            clock,
            magic_number=settings.account.magic_number,
            max_send_retries=settings.execution.max_send_retries,
            unknown_outcome_wait_s=settings.execution.unknown_outcome_wait_s,
            code_version="replay",
            settings_version="replay",
            commission_per_lot_side={
                symbol: settings.symbols[symbol].commission_per_lot_side
                for symbol in enabled_symbols
            },
            wait=lambda _: None,
        ),
        clock,
        code_version="replay",
        settings_version="replay",
        news_enabled=not no_news,
        strategy_mode=strategy_mode,
    )

    decisions: list[Decision] = []
    equity_curve = [EquityPoint(first_time, simulated_account.equity)]
    try:
        engine.start()
        events = sorted(
            {
                candle.time
                for candles in selected_histories.values()
                for candle in candles
            }
        )
        for at_utc in events:
            clock.set(at_utc)
            _update_market(broker, selected_histories, enabled_symbols, at_utc)
            decisions.extend(engine.run_entry_lane())
            for symbol in enabled_symbols:
                candle = _candle_at(selected_histories[(symbol, "H1")], at_utc)
                if candle is not None:
                    broker.process_candle(symbol, candle)
            management = engine.run_management_lane()
            if management.degraded:
                raise RuntimeError(f"Simulated broker degraded at {at_utc.isoformat()}")
            account_now = broker.account()
            equity_curve.append(EquityPoint(at_utc, account_now.equity))
            if any(
                position.stop_loss <= 0
                for position in broker.positions(settings.account.magic_number)
            ):
                raise RuntimeError("Replay produced a position without a protective stop")

        final_account = broker.account()
        deals = broker.history_deals(first_time, settings.account.magic_number)
        positions = broker.positions(settings.account.magic_number)
        open_positions = tuple(
            {
                "ticket": position.ticket,
                "symbol": position.symbol,
                "side": position.side.value,
                "lots": position.lots,
                "entry": position.entry,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "opened_at_utc": position.opened_at_utc.isoformat(),
            }
            for position in positions
        )
        result = ReplayRunResult(
            strategy_mode=strategy_mode,
            starting_equity=starting_equity,
            ending_equity=final_account.equity,
            decisions=tuple(decisions),
            deals=deals,
            equity_curve=tuple(equity_curve),
            open_positions=open_positions,
            excluded_recent_bars=excluded_recent_bars,
            news_enabled=not no_news,
            final_run=final,
        )
        if report_root is not None:
            result = replace(
                result,
                report_dir=write_replay_report(
                    report_root, result, created_at_utc=now
                ),
            )
        return result
    finally:
        engine.stop()
        store.close_thread_connection()
        temp_dir.cleanup()


def write_replay_report(
    report_root: str | Path,
    result: ReplayRunResult,
    *,
    created_at_utc: datetime,
) -> Path:
    created = _require_utc(created_at_utc, "created_at_utc")
    run_dir = Path(report_root) / f"{created:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    deals_by_position: dict[str, list[BrokerDeal]] = defaultdict(list)
    for deal in result.deals:
        deals_by_position[deal.position_ticket].append(deal)

    _write_csv(
        run_dir / "trades.csv",
        (
            "ticket",
            "symbol",
            "side",
            "lots",
            "entry_time_utc",
            "exit_time_utc",
            "entry_price",
            "exit_price",
            "profit",
            "commission",
            "open",
        ),
        (
            _trade_row(ticket, items)
            for ticket, items in sorted(deals_by_position.items())
        ),
    )
    _write_csv(
        run_dir / "decisions.csv",
        ("time_utc", "symbol", "reason", "strategy", "side", "entry", "stop_loss", "take_profit"),
        (_decision_row(decision) for decision in result.decisions),
    )
    _write_csv(
        run_dir / "equity_curve.csv",
        ("time_utc", "equity"),
        (
            {"time_utc": point.time_utc.isoformat(), "equity": point.equity}
            for point in result.equity_curve
        ),
    )
    summary = {
        "schema_version": 1,
        "created_at_utc": created.isoformat(),
        "strategy": result.strategy_mode,
        "news_enabled": result.news_enabled,
        "final_run": result.final_run,
        "starting_equity": result.starting_equity,
        "ending_equity": result.ending_equity,
        "net_profit": result.ending_equity - result.starting_equity,
        "excluded_recent_bars": result.excluded_recent_bars,
        "decision_count": len(result.decisions),
        "trade_count": sum(
            any(deal.is_entry is True for deal in items)
            and any(deal.is_entry is False for deal in items)
            for items in deals_by_position.values()
        ),
        "open_positions": list(result.open_positions),
    }
    temp_path = run_dir / "summary.json.tmp"
    summary_path = run_dir / "summary.json"
    try:
        temp_path.write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, summary_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return run_dir


def _update_market(
    broker: SimBroker,
    histories: Mapping[tuple[str, Timeframe], tuple[Candle, ...]],
    symbols: tuple[str, ...],
    at_utc: datetime,
) -> None:
    for symbol in symbols:
        h1_history = histories[(symbol, "H1")]
        current_h1 = tuple(candle for candle in h1_history if candle.time <= at_utc)
        broker.candle_data[(symbol, "H1")] = current_h1
        current_candle = current_h1[-1] if current_h1 else h1_history[0]
        broker.tick_data[symbol] = _tick_for_candle(current_candle)
        for timeframe in ("H1", "H4"):
            key = (symbol, timeframe)
            if key in histories:
                broker.candle_data[key] = tuple(
                    candle for candle in histories[key] if candle.time <= at_utc
                )


def _tick_for_candle(candle: Candle) -> Tick:
    spread = candle.spread
    return Tick(candle.time, candle.open, candle.open + spread)


def _candle_at(candles: tuple[Candle, ...], at_utc: datetime) -> Candle | None:
    return next((candle for candle in candles if candle.time == at_utc), None)


def _trade_row(ticket: str, deals: list[BrokerDeal]) -> dict[str, object]:
    entry = next((deal for deal in deals if deal.is_entry is True), None)
    exit_deal = next((deal for deal in reversed(deals) if deal.is_entry is False), None)
    reference = entry or deals[0]
    return {
        "ticket": ticket,
        "symbol": reference.symbol,
        "side": reference.side.value if entry is not None else "",
        "lots": reference.lots,
        "entry_time_utc": entry.time_utc.isoformat() if entry else "",
        "exit_time_utc": exit_deal.time_utc.isoformat() if exit_deal else "",
        "entry_price": entry.price if entry else "",
        "exit_price": exit_deal.price if exit_deal else "",
        "profit": sum(deal.profit for deal in deals),
        "commission": sum(deal.commission for deal in deals),
        "open": exit_deal is None,
    }


def _decision_row(decision: Decision) -> dict[str, object]:
    proposal = decision.proposal
    return {
        "time_utc": decision.time.isoformat(),
        "symbol": decision.symbol,
        "reason": decision.reason.value,
        "strategy": decision.strategy or "",
        "side": proposal.side.value if proposal else "",
        "entry": proposal.entry if proposal else "",
        "stop_loss": proposal.stop_loss if proposal else "",
        "take_profit": proposal.take_profit if proposal else "",
    }


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: Iterable[dict[str, object]],
) -> None:
    temp_path = path.with_suffix(".csv.tmp")
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
