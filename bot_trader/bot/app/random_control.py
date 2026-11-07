from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from random import Random
from typing import Literal

from bot.core.indicators import atr, bollinger
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from bot.core.types import Candle, Proposal, ReasonCode, Side, StrategyResult

StrategyMode = Literal["a", "b"]
RandomEntryKey = tuple[str, str, datetime]


def build_random_entry_schedule(
    histories: Mapping[tuple[str, str], tuple[Candle, ...]],
    trade_targets: Mapping[str, int],
    strategy: StrategyMode,
    generator: Random,
    *,
    candidates_per_trade: int = 1,
) -> dict[RandomEntryKey, Side]:
    if candidates_per_trade < 1:
        raise ValueError("candidates_per_trade must be positive")
    schedule: dict[RandomEntryKey, Side] = {}
    sides = (Side.LONG, Side.SHORT)
    for symbol, target in trade_targets.items():
        if target < 0:
            raise ValueError("trade targets cannot be negative")
        if target == 0:
            continue
        candidates: list[RandomEntryKey] = []
        timeframes = ("H1", "H4") if strategy == "a" else ("H1",)
        minimum_candles = (
            StrategyAParams().min_candles
            if strategy == "a"
            else StrategyBParams().min_candles
        )
        for timeframe in timeframes:
            candles = histories.get((symbol, timeframe), ())
            candidates.extend(
                (symbol, timeframe, candle.time)
                for candle in candles[minimum_candles - 1 :]
            )
        count = min(len(candidates), target * candidates_per_trade)
        if count == 0:
            raise ValueError(
                f"no random-entry candidates for {symbol} Strategy {strategy.upper()}"
            )
        for key in generator.sample(candidates, count):
            schedule[key] = generator.choice(sides)
    return schedule


def propose_random_entry(
    candles: tuple[Candle, ...],
    strategy: StrategyMode,
    side: Side,
    params: StrategyAParams | StrategyBParams,
) -> StrategyResult:
    if strategy == "a":
        if not isinstance(params, StrategyAParams):
            raise TypeError("Strategy A random entries require StrategyAParams")
        active = params
    else:
        if not isinstance(params, StrategyBParams):
            raise TypeError("Strategy B random entries require StrategyBParams")
        active = params
    if len(candles) < active.min_candles:
        return StrategyResult(None, ReasonCode.WARMUP)

    atr_values = atr(candles, active.atr_period)
    current_atr = atr_values[-1]
    if current_atr <= 0:
        return StrategyResult(None, ReasonCode.WARMUP)
    current = candles[-1]
    entry = current.close
    if side is Side.LONG:
        stop = min(candle.low for candle in candles[-active.stop_lookback :]) - (
            active.stop_buffer_atr * current_atr
        )
        distance = entry - stop
        if distance < active.min_stop_atr * current_atr:
            distance = active.min_stop_atr * current_atr
            stop = entry - distance
    else:
        stop = max(candle.high for candle in candles[-active.stop_lookback :]) + (
            active.stop_buffer_atr * current_atr
        )
        distance = stop - entry
        if distance < active.min_stop_atr * current_atr:
            distance = active.min_stop_atr * current_atr
            stop = entry + distance
    if distance > active.max_stop_atr * current_atr:
        return StrategyResult(None, ReasonCode.STOP_TOO_WIDE)

    if strategy == "a":
        target = (
            entry + active.take_profit_r * distance
            if side is Side.LONG
            else entry - active.take_profit_r * distance
        )
    else:
        middle, _, _ = bollinger(
            [candle.close for candle in candles],
            active.bollinger_period,
            active.bollinger_deviations,
        )
        target = middle[-1]
        if target is None:
            return StrategyResult(None, ReasonCode.WARMUP)
        reward = target - entry if side is Side.LONG else entry - target
        if reward < active.min_reward_r * distance:
            return StrategyResult(None, ReasonCode.REWARD_TOO_SMALL)

    return StrategyResult(
        Proposal(side, entry, stop, target, "RANDOM_CONTROL"),
        None,
    )
