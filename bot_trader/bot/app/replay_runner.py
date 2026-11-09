from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Literal
from uuid import uuid4

from bot.adapters.broker import BrokerAccount, BrokerDeal, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.sim_broker import SimBroker, SimCosts
from bot.adapters.store import StateStore, TradeRecord
from bot.app.backtest import EquityPoint, select_backtest_history
from bot.app.config import (
    Settings,
    build_simulator_costs,
    schedule_validation_failures,
)
from bot.app.data import DataQualityError, Timeframe
from bot.app.engine import Engine
from bot.app.executor import Executor
from bot.app.news import NewsEvent
from bot.app.random_control import RandomEntryKey
from bot.core.clock import SimClock
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from bot.core.timeutil import expected_new_york_offset
from bot.core.types import Candle, Decision, Side, SymbolSpec

StrategyMode = Literal["a", "b", "both"]
HistoryMap = Mapping[tuple[str, Timeframe], tuple[Candle, ...]]
DEFAULT_STARTING_EQUITY = 3_000.0


@dataclass(frozen=True, slots=True)
class ReplayRunResult:
    strategy_mode: StrategyMode
    strategy_parameters: dict[str, dict[str, int | float]]
    random_entry_targets: dict[str, int] | None
    news_events: tuple[NewsEvent, ...]
    news_calendar_valid: bool | None
    news_block_before_min: int
    news_block_after_min: int
    starting_equity: float
    ending_equity: float
    cost_multiplier: float
    code_version: str
    settings_version: str
    decisions: tuple[Decision, ...]
    deals: tuple[BrokerDeal, ...]
    trades: tuple[TradeRecord, ...]
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
    code_version: str = "replay",
    settings_version: str = "replay",
    cost_multiplier: float = 1.0,
    strategy_a_params: StrategyAParams | None = None,
    strategy_b_params: StrategyBParams | None = None,
    random_entry_schedule: Mapping[RandomEntryKey, Side] | None = None,
    random_entry_targets: Mapping[str, int] | None = None,
) -> ReplayRunResult:
    """Replay cached candles through the production Engine, Executor and SimBroker."""
    now = _require_utc(now_utc, "now_utc")
    if strategy_mode not in ("a", "b", "both"):
        raise ValueError("strategy_mode must be 'a', 'b', or 'both'")
    if strategy_a_params is not None and strategy_mode != "a":
        raise ValueError("Strategy A parameter overrides require strategy_mode='a'")
    if strategy_b_params is not None and strategy_mode != "b":
        raise ValueError("Strategy B parameter overrides require strategy_mode='b'")
    if (random_entry_schedule is None) != (random_entry_targets is None):
        raise ValueError("Random-entry schedule and targets must be provided together")
    if random_entry_schedule is not None and strategy_mode == "both":
        raise ValueError("Random-entry control requires strategy_mode='a' or 'b'")
    if random_entry_targets is not None and any(
        target < 0 for target in random_entry_targets.values()
    ):
        raise ValueError("Random-entry targets cannot be negative")
    if settings.account.mode != "demo" or settings.account.allow_live:
        raise ValueError("Replay requires demo-only settings with live trading disabled")
    if starting_equity <= 0 or not isfinite(starting_equity):
        raise ValueError("starting_equity must be positive")
    if cost_multiplier <= 0 or not isfinite(cost_multiplier):
        raise ValueError("cost_multiplier must be positive and finite")

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
        symbol: _tick_for_candle(candle, spread_multiplier=cost_multiplier)
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
        costs=_simulator_costs(settings, enabled_symbols, cost_multiplier),
        server_offset_hours=expected_new_york_offset(first_time),
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
            code_version=code_version,
            settings_version=settings_version,
            commission_per_lot_side={
                symbol: settings.symbols[symbol].commission_per_lot_side
                for symbol in enabled_symbols
            },
            wait=lambda _: None,
        ),
        clock,
        code_version=code_version,
        settings_version=settings_version,
        news_enabled=not no_news,
        strategy_mode=strategy_mode,
        strategy_a_params=strategy_a_params,
        strategy_b_params=strategy_b_params,
        random_entry_schedule=random_entry_schedule,
        random_entry_targets=random_entry_targets,
    )

    decisions: list[Decision] = []
    equity_curve = [EquityPoint(first_time, simulated_account.equity)]
    try:
        engine.start()
        replay_news_events = engine.news_events
        replay_news_calendar_valid = engine.news_calendar_valid
        events = sorted(
            {
                candle.time
                for candles in selected_histories.values()
                for candle in candles
            }
        )
        for at_utc in events:
            clock.set(at_utc)
            _update_market(
                broker,
                selected_histories,
                enabled_symbols,
                at_utc,
                spread_multiplier=cost_multiplier,
            )
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
        trades = store.trades()
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
            strategy_parameters={
                "A": asdict(strategy_a_params or settings.strategy_a.to_params())
            }
            if strategy_mode == "a"
            else {
                "B": asdict(strategy_b_params or settings.strategy_b.to_params())
            }
            if strategy_mode == "b"
            else {
                "A": asdict(settings.strategy_a.to_params()),
                "B": asdict(settings.strategy_b.to_params()),
            },
            random_entry_targets=(
                dict(random_entry_targets)
                if random_entry_targets is not None
                else None
            ),
            news_events=replay_news_events,
            news_calendar_valid=replay_news_calendar_valid,
            news_block_before_min=settings.news.block_before_min,
            news_block_after_min=settings.news.block_after_min,
            starting_equity=starting_equity,
            ending_equity=final_account.equity,
            cost_multiplier=cost_multiplier,
            code_version=code_version,
            settings_version=settings_version,
            decisions=tuple(decisions),
            deals=deals,
            trades=trades,
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
    trade_by_ticket = {trade.ticket: trade for trade in result.trades}

    _write_csv(
        run_dir / "trades.csv",
        (
            "ticket",
            "symbol",
            "strategy",
            "timeframe",
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
            _trade_row(ticket, items, trade_by_ticket.get(ticket))
            for ticket, items in sorted(deals_by_position.items())
        ),
    )
    _write_csv(
        run_dir / "portfolio_breakdown.csv",
        (
            "grouping",
            "instrument",
            "timeframe",
            "trade_count",
            "closed_trades",
            "open_trades",
            "net_profit",
        ),
        _portfolio_breakdown(result, deals_by_position),
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
        "strategy_parameters": result.strategy_parameters,
        "random_entry_control": result.random_entry_targets is not None,
        "random_entry_targets": result.random_entry_targets,
        "news_calendar_valid": result.news_calendar_valid,
        "news_block_before_min": result.news_block_before_min,
        "news_block_after_min": result.news_block_after_min,
        "news_events": [
            {
                "time_utc": event.time_utc.isoformat(),
                "currency": event.currency,
                "impact": event.impact,
                "title": event.title,
            }
            for event in result.news_events
        ],
        "news_enabled": result.news_enabled,
        "final_run": result.final_run,
        "cost_multiplier": result.cost_multiplier,
        "code_version": result.code_version,
        "settings_version": result.settings_version,
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


def claim_final_holdout_lock(
    lock_path: str | Path,
    *,
    code_version: str,
    created_at_utc: datetime,
) -> Path:
    created = _require_utc(created_at_utc, "created_at_utc")
    if not code_version.strip():
        raise ValueError("code_version must not be empty")
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "code_version": code_version,
        "created_at_utc": created.isoformat(),
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ValueError(f"Final holdout is already locked at {path}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
        lock_file.write(json.dumps(payload, indent=2, allow_nan=False) + "\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
    return path


def _update_market(
    broker: SimBroker,
    histories: Mapping[tuple[str, Timeframe], tuple[Candle, ...]],
    symbols: tuple[str, ...],
    at_utc: datetime,
    *,
    spread_multiplier: float,
) -> None:
    for symbol in symbols:
        broker.server_offset_hours = expected_new_york_offset(at_utc)
        h1_history = histories[(symbol, "H1")]
        current_h1 = tuple(candle for candle in h1_history if candle.time <= at_utc)
        broker.candle_data[(symbol, "H1")] = current_h1
        current_candle = current_h1[-1] if current_h1 else h1_history[0]
        broker.tick_data[symbol] = _tick_for_candle(
            current_candle,
            spread_multiplier=spread_multiplier,
        )
        for timeframe in ("H1", "H4"):
            key = (symbol, timeframe)
            if key in histories:
                broker.candle_data[key] = tuple(
                    candle for candle in histories[key] if candle.time <= at_utc
                )


def _tick_for_candle(candle: Candle, *, spread_multiplier: float = 1.0) -> Tick:
    spread = candle.spread * spread_multiplier
    return Tick(candle.time, candle.open, candle.open + spread)


def _simulator_costs(
    settings: Settings,
    symbols: tuple[str, ...],
    cost_multiplier: float,
) -> dict[str, SimCosts]:
    return {
        symbol: replace(
            cost,
            slippage_points=cost.slippage_points * cost_multiplier,
        )
        for symbol, cost in build_simulator_costs(settings, symbols).items()
    }


def _candle_at(candles: tuple[Candle, ...], at_utc: datetime) -> Candle | None:
    return next((candle for candle in candles if candle.time == at_utc), None)


def _trade_row(
    ticket: str,
    deals: list[BrokerDeal],
    trade: TradeRecord | None,
) -> dict[str, object]:
    entry = next((deal for deal in deals if deal.is_entry is True), None)
    exit_deal = next((deal for deal in reversed(deals) if deal.is_entry is False), None)
    reference = entry or deals[0]
    return {
        "ticket": ticket,
        "symbol": reference.symbol,
        "strategy": trade.strategy if trade is not None else "",
        "timeframe": trade.timeframe if trade is not None else "",
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


def _portfolio_breakdown(
    result: ReplayRunResult,
    deals_by_position: Mapping[str, list[BrokerDeal]],
) -> Iterable[dict[str, object]]:
    trades_by_bucket: dict[tuple[str, str, str], list[TradeRecord]] = defaultdict(list)
    for trade in result.trades:
        trades_by_bucket[(trade.symbol, trade.timeframe, trade.strategy)].append(trade)

    def row(
        grouping: str,
        instrument: str,
        timeframe: str,
        bucket_trades: list[TradeRecord],
    ) -> dict[str, object]:
        profit = 0.0
        closed_count = 0
        open_count = 0
        for trade in bucket_trades:
            deals = deals_by_position.get(trade.ticket, [])
            is_closed = any(deal.is_entry is False for deal in deals)
            if is_closed:
                closed_count += 1
                profit += sum(deal.profit - deal.commission for deal in deals)
            else:
                open_count += 1
        return {
            "grouping": grouping,
            "instrument": instrument,
            "timeframe": timeframe,
            "trade_count": len(bucket_trades),
            "closed_trades": closed_count,
            "open_trades": open_count,
            "net_profit": profit,
        }

    for (symbol, timeframe, strategy), trades in sorted(trades_by_bucket.items()):
        yield row("strategy", symbol, f"{timeframe}:{strategy}", trades)
        yield row("instrument_timeframe", symbol, timeframe, trades)
    if result.trades:
        yield row("combined_A+B", "ALL", "ALL", list(result.trades))


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
