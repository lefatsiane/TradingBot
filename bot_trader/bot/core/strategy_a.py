#strategy_a.py
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from bot.core.indicators import atr, ema
from bot.core.types import Candle, Proposal, ReasonCode, Side, StrategyResult


@dataclass(frozen=True, slots=True)
class StrategyAParams:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    slope_lookback: int = 20
    pullback_window: int = 5
    stop_lookback: int = 5
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    take_profit_r: float = 2.0
    min_candles: int = 300
    window_candles: int = 1_000
    cooldown_bars_after_stop: int = 6

    def __post_init__(self) -> None:
        integer_values = (
            self.ema_fast,
            self.ema_mid,
            self.ema_slow,
            self.atr_period,
            self.slope_lookback,
            self.pullback_window,
            self.stop_lookback,
            self.min_candles,
            self.window_candles,
            self.cooldown_bars_after_stop,
        )
        if any(value < 1 for value in integer_values):
            raise ValueError("Strategy A periods and candle limits must be positive")
        if not self.ema_fast < self.ema_mid < self.ema_slow:
            raise ValueError("Strategy A EMA periods must be strictly increasing")
        if self.window_candles < self.min_candles:
            raise ValueError("Strategy A window must be at least its warm-up length")
        if self.min_candles <= self.slope_lookback:
            raise ValueError("Strategy A warm-up must exceed its slope lookback")
        float_values = (
            self.stop_buffer_atr,
            self.min_stop_atr,
            self.max_stop_atr,
            self.take_profit_r,
        )
        if not all(isfinite(value) for value in float_values):
            raise ValueError("Strategy A parameters must be finite")
        if (
            self.stop_buffer_atr < 0
            or self.min_stop_atr <= 0
            or self.max_stop_atr < self.min_stop_atr
            or self.take_profit_r <= 0
        ):
            raise ValueError("Strategy A stop and target parameters are invalid")


def propose(
    candles: Sequence[Candle], params: StrategyAParams
) -> StrategyResult:
    """Evaluate Strategy A from closed candles and explicit parameter values."""
    history = list(candles[-params.window_candles :])
    if len(history) < params.min_candles:
        return StrategyResult(None, ReasonCode.WARMUP)

    closes = [candle.close for candle in history]
    ema_fast = ema(closes, params.ema_fast)
    ema_mid = ema(closes, params.ema_mid)
    ema_slow = ema(closes, params.ema_slow)
    average_true_range = atr(history, params.atr_period)
    current_atr = average_true_range[-1]

    current = history[-1]
    previous = history[-2]
    indicator_values = (
        (f"ema{params.ema_fast}", ema_fast[-1]),
        (f"ema{params.ema_mid}", ema_mid[-1]),
        (f"ema{params.ema_slow}", ema_slow[-1]),
        (f"atr{params.atr_period}", current_atr),
    )

    long_trend = (
        current.close > ema_slow[-1]
        and ema_mid[-1] > ema_slow[-1]
        and ema_slow[-1] > ema_slow[-params.slope_lookback - 1]
    )
    short_trend = (
        current.close < ema_slow[-1]
        and ema_mid[-1] < ema_slow[-1]
        and ema_slow[-1] < ema_slow[-params.slope_lookback - 1]
    )
    if not long_trend and not short_trend:
        return StrategyResult(None, ReasonCode.NO_TREND, indicator_values)

    pullback_start = len(history) - params.pullback_window - 1
    pullback = history[pullback_start:-1]
    pullback_ema_fast = ema_fast[pullback_start:-1]
    pullback_ema_mid = ema_mid[pullback_start:-1]
    if long_trend:
        valid_pullback = any(
            candle.low <= average
            for candle, average in zip(pullback, pullback_ema_fast, strict=True)
        ) and all(
            candle.close >= average
            for candle, average in zip(pullback, pullback_ema_mid, strict=True)
        )
        if not valid_pullback:
            return StrategyResult(None, ReasonCode.NO_PULLBACK, indicator_values)
        if current.close <= ema_fast[-1] or current.close <= previous.high:
            return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)

        entry = current.close
        stop = min(candle.low for candle in history[-params.stop_lookback :]) - (
            params.stop_buffer_atr * current_atr
        )
        distance = entry - stop
        if distance < params.min_stop_atr * current_atr:
            distance = params.min_stop_atr * current_atr
            stop = entry - distance
        if distance > params.max_stop_atr * current_atr:
            return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
        target = entry + params.take_profit_r * distance
        return StrategyResult(
            Proposal(Side.LONG, entry, stop, target, "STRATEGY_A", indicator_values),
            None,
            indicator_values,
        )

    valid_pullback = any(
        candle.high >= average
        for candle, average in zip(pullback, pullback_ema_fast, strict=True)
    ) and all(
        candle.close <= average
        for candle, average in zip(pullback, pullback_ema_mid, strict=True)
    )
    if not valid_pullback:
        return StrategyResult(None, ReasonCode.NO_PULLBACK, indicator_values)
    if current.close >= ema_fast[-1] or current.close >= previous.low:
        return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)

    entry = current.close
    stop = max(candle.high for candle in history[-params.stop_lookback :]) + (
        params.stop_buffer_atr * current_atr
    )
    distance = stop - entry
    if distance < params.min_stop_atr * current_atr:
        distance = params.min_stop_atr * current_atr
        stop = entry + distance
    if distance > params.max_stop_atr * current_atr:
        return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
    target = entry - params.take_profit_r * distance
    return StrategyResult(
        Proposal(Side.SHORT, entry, stop, target, "STRATEGY_A", indicator_values),
        None,
        indicator_values,
    )
