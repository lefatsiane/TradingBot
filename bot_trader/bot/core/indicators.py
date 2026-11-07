"""Technical indicator helpers used throughout the trading bot."""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

from bot.core.types import Candle


def _finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def ema(values: Sequence[float], period: int) -> list[float]:
    if period < 1:
        raise ValueError("period must be at least 1")
    if not values:
        return []
    if not _finite(values):
        raise ValueError("indicator inputs must be finite")

    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    result: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        if previous_close is None:
            result.append(candle.high - candle.low)
        else:
            result.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - previous_close),
                    abs(candle.low - previous_close),
                )
            )
        previous_close = candle.close
    return result


def wilder(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be at least 1")
    if not _finite(values):
        raise ValueError("indicator inputs must be finite")

    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    average = sum(values[:period]) / period
    result[period - 1] = average
    for index in range(period, len(values)):
        average = (average * (period - 1) + values[index]) / period
        result[index] = average
    return result


def atr(candles: Sequence[Candle], period: int = 14) -> list[float]:
    if period < 1:
        raise ValueError("period must be at least 1")

    ranges = true_ranges(candles)
    if not ranges:
        return []

    alpha = 1.0 / period
    result = [ranges[0]]
    for value in ranges[1:]:
        previous = result[-1]
        result.append(alpha * value + (1.0 - alpha) * previous)
    return result


def bollinger(
    values: Sequence[float],
    period: int = 20,
    deviations: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    if period < 1 or deviations < 0:
        raise ValueError("invalid Bollinger parameters")

    middle: list[float | None] = [None] * len(values)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)

    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        width = deviations * math.sqrt(variance)
        middle[index] = mean
        upper[index] = mean + width
        lower[index] = mean - width
    return middle, upper, lower


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be at least 1")

    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calculate(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result[period] = calculate(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change_index = index - 1
        avg_gain = (avg_gain * (period - 1) + gains[change_index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[change_index]) / period
        result[index] = calculate(avg_gain, avg_loss)
    return result


def adx(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be at least 1")

    result: list[float | None] = [None] * len(candles)
    if len(candles) < 2 * period:
        return result

    plus_dm = [0.0]
    minus_dm = [0.0]
    for previous, current in pairwise(candles):
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    atr_values = wilder(true_ranges(candles), period)
    plus_values = wilder(plus_dm, period)
    minus_values = wilder(minus_dm, period)
    dx: list[float | None] = [None] * len(candles)
    for index, (a, plus, minus) in enumerate(zip(atr_values, plus_values, minus_values)):
        if a is None or a == 0 or plus is None or minus is None:
            continue
        plus_di = 100.0 * plus / a
        minus_di = 100.0 * minus / a
        denominator = plus_di + minus_di
        dx[index] = 0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator

    first_dx = 2 * period - 2
    initial = [value for value in dx[period - 1 : first_dx + 1] if value is not None]
    if len(initial) < period:
        return result

    current_adx = sum(initial) / period
    result[first_dx] = current_adx
    for index in range(first_dx + 1, len(candles)):
        value = dx[index]
        if value is not None:
            current_adx = (current_adx * (period - 1) + value) / period
            result[index] = current_adx
    return result
