from __future__ import annotations

import json
import math
import random
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from bot.adapters.broker import SymbolSpec
from bot.app.backtest import select_backtest_history
from bot.app.config import Settings
from bot.app.data import Candle, Timeframe
from bot.app.random_control import build_random_entry_schedule
from bot.app.replay_runner import HistoryMap, run_replay

RandomStrategy = Literal["a", "b"]
DEFAULT_RANDOM_RUNS = 1_000
RANDOM_RETRIES = 20
RANDOM_CANDIDATES_PER_TARGET = 100


def run_random_entry_control(
    histories: HistoryMap,
    specs: Mapping[str, SymbolSpec],
    settings: Settings,
    *,
    strategies: tuple[RandomStrategy, ...],
    now_utc: datetime,
    starting_equity: float,
    no_news: bool,
    report_root: str | Path,
    code_version: str,
    settings_version: str,
    cost_multiplier: float = 1.0,
    runs: int = DEFAULT_RANDOM_RUNS,
    seed: int = 0,
) -> Path:
    if not strategies or any(strategy not in ("a", "b") for strategy in strategies):
        raise ValueError("strategies must contain 'a', 'b', or both")
    if len(set(strategies)) != len(strategies):
        raise ValueError("strategies cannot contain duplicates")
    if runs < 1:
        raise ValueError("runs must be positive")
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    now = now_utc.astimezone(UTC)
    report_directory = Path(report_root)
    report_directory.mkdir(parents=True, exist_ok=True)
    strategy_results: dict[str, dict[str, Any]] = {}

    for strategy in strategies:
        baseline = run_replay(
            histories,
            specs,
            settings,
            now_utc=now,
            starting_equity=starting_equity,
            no_news=no_news,
            strategy_mode=strategy,
            code_version=code_version,
            settings_version=settings_version,
            cost_multiplier=cost_multiplier,
        )
        targets = Counter(trade.symbol for trade in baseline.trades)
        if not targets:
            strategy_results[strategy] = {
                "status": "INCONCLUSIVE",
                "reason": "baseline contains no trades",
                "trade_targets": {},
                "baseline_expectancy_r": None,
                "random_expectancy_95th_percentile_r": None,
                "completed_runs": 0,
                "requested_runs": runs,
                "seed": seed,
                "runs": [],
            }
            continue

        baseline_expectancy = _expectancy_r(baseline.trades, baseline.deals)
        if baseline_expectancy is None:
            strategy_results[strategy] = {
                "status": "INCONCLUSIVE",
                "reason": "baseline has no closed trades with recorded R results",
                "trade_targets": dict(targets),
                "baseline_expectancy_r": None,
                "random_expectancy_95th_percentile_r": None,
                "completed_runs": 0,
                "requested_runs": runs,
                "seed": seed,
                "runs": [],
            }
            continue
        eligible_histories: dict[
            tuple[str, Timeframe], tuple[Candle, ...]
        ] = {}
        expected_timeframes = ("H1", "H4") if strategy == "a" else ("H1",)
        for key, candles in histories.items():
            _, timeframe = key
            if timeframe in expected_timeframes:
                eligible_histories[key] = select_backtest_history(
                    candles,
                    timeframe,
                    now_utc=now,
                    final=False,
                )[0]
        period_candles = [
            candle for candles in eligible_histories.values() for candle in candles
        ]
        random_values: list[float] = []
        run_rows: list[dict[str, Any]] = []
        for run_number in range(runs):
            matched = None
            attempts = 0
            for attempt in range(RANDOM_RETRIES):
                attempts = attempt + 1
                run_seed = seed + (run_number * RANDOM_RETRIES) + attempt
                schedule = build_random_entry_schedule(
                    eligible_histories,
                    targets,
                    strategy,
                    random.Random(run_seed),
                    candidates_per_trade=RANDOM_CANDIDATES_PER_TARGET,
                )
                outcome = run_replay(
                    histories,
                    specs,
                    settings,
                    now_utc=now,
                    starting_equity=starting_equity,
                    no_news=no_news,
                    strategy_mode=strategy,
                    code_version=code_version,
                    settings_version=settings_version,
                    cost_multiplier=cost_multiplier,
                    random_entry_schedule=schedule,
                    random_entry_targets=targets,
                )
                actual = Counter(trade.symbol for trade in outcome.trades)
                if actual == targets:
                    matched = outcome
                    break
            if matched is None:
                run_rows.append(
                    {
                        "run": run_number + 1,
                        "status": "TRADE_COUNT_MISMATCH",
                        "seed": run_seed,
                        "attempts": attempts,
                        "actual_trade_counts": dict(actual),
                        "expectancy_r": None,
                    }
                )
                continue
            expectancy = _expectancy_r(matched.trades, matched.deals)
            if expectancy is None:
                run_rows.append(
                    {
                        "run": run_number + 1,
                        "status": "NO_CLOSED_TRADES",
                        "seed": run_seed,
                        "attempts": attempts,
                        "actual_trade_counts": dict(targets),
                        "expectancy_r": None,
                    }
                )
            else:
                random_values.append(expectancy)
                run_rows.append(
                    {
                        "run": run_number + 1,
                        "status": "COMPLETED",
                        "seed": run_seed,
                        "attempts": attempts,
                        "actual_trade_counts": dict(targets),
                        "expectancy_r": expectancy,
                    }
                )

        percentile = (
            _percentile(random_values, 0.95) if random_values else None
        )
        complete = (
            runs == DEFAULT_RANDOM_RUNS
            and len(random_values) == DEFAULT_RANDOM_RUNS
        )
        status = (
            "INCONCLUSIVE"
            if not complete or baseline_expectancy is None or percentile is None
            else "PASS"
            if baseline_expectancy > percentile
            else "FAIL"
        )
        strategy_results[strategy] = {
            "status": status,
            "history_start_utc": (
                min(candle.time for candle in period_candles).isoformat()
                if period_candles
                else None
            ),
            "history_end_utc": (
                max(candle.time for candle in period_candles).isoformat()
                if period_candles
                else None
            ),
            "trade_targets": dict(targets),
            "baseline_expectancy_r": baseline_expectancy,
            "baseline_closed_trade_count": sum(
                trade.closed_at_utc is not None for trade in baseline.trades
            ),
            "random_expectancy_95th_percentile_r": percentile,
            "completed_runs": len(random_values),
            "requested_runs": runs,
            "acceptance_run_count_met": runs == DEFAULT_RANDOM_RUNS,
            "seed": seed,
            "runs": run_rows,
        }

    payload = {
        "schema_version": 1,
        "created_at_utc": now.isoformat(),
        "code_version": code_version,
        "settings_version": settings_version,
        "no_news": no_news,
        "cost_multiplier": cost_multiplier,
        "holdout_final": False,
        "requested_runs_per_strategy": runs,
        "strategies": strategy_results,
    }
    destination = (
        report_directory
        / f"random-control-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.json"
    )
    temporary = destination.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _expectancy_r(
    trades: tuple[Any, ...],
    deals: tuple[Any, ...],
) -> float | None:
    deals_by_position: dict[str, list[Any]] = {}
    for deal in deals:
        deals_by_position.setdefault(deal.position_ticket, []).append(deal)
    values: list[float] = []
    for trade in trades:
        trade_deals = deals_by_position.get(trade.ticket, [])
        if (
            trade.actual_risk <= 0
            or not any(deal.is_entry is False for deal in trade_deals)
        ):
            continue
        net_profit = sum(deal.profit - deal.commission for deal in trade_deals)
        result_r = net_profit / trade.actual_risk
        if math.isfinite(result_r):
            values.append(result_r)
    return sum(values) / len(values) if values else None


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)
