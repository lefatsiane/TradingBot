from __future__ import annotations

from collections.abc import Sequence

from bot.core.indicators import adx, atr, bollinger, rsi
from bot.core.types import Candle, Proposal, ReasonCode, Side, StrategyResult

WINDOW = 1_000
MIN_CANDLES = 300


def propose(candles: Sequence[Candle], *, timeframe: str = "H1") -> StrategyResult:
    """Evaluate the fixed H1 quiet-market mean-reversion strategy."""
    if timeframe != "H1":
        raise ValueError("Strategy B is defined for H1 candles only")
    history = list(candles[-WINDOW:])
    if len(history) < MIN_CANDLES:
        return StrategyResult(None, ReasonCode.WARMUP)

    closes = [candle.close for candle in history]
    middle, upper, lower = bollinger(closes, 20, 2.0)
    rsi14 = rsi(closes, 14)
    adx14 = adx(history, 14)
    atr14 = atr(history, 14)

    if any(value is None for value in (middle[-1], lower[-1], upper[-1], rsi14[-2], adx14[-1], atr14[-1])):
        return StrategyResult(None, ReasonCode.WARMUP)

    mid = middle[-1]
    band_low = lower[-1]
    band_high = upper[-1]
    prior_rsi = rsi14[-2]
    current_adx = adx14[-1]
    current_atr = atr14[-1]
    assert mid is not None and band_low is not None and band_high is not None
    assert prior_rsi is not None and current_adx is not None and current_atr is not None

    indicator_values = (
        ("bb_middle", mid),
        ("bb_lower", band_low),
        ("bb_upper", band_high),
        ("rsi14_previous", prior_rsi),
        ("adx14", current_adx),
        ("atr14", current_atr),
    )
    if current_adx >= 20.0:
        return StrategyResult(None, ReasonCode.NO_QUIET_MARKET, indicator_values)

    previous = history[-2]
    current = history[-1]
    previous_lower = lower[-2]
    previous_upper = upper[-2]
    if previous_lower is None or previous_upper is None:
        return StrategyResult(None, ReasonCode.WARMUP, indicator_values)

    long_setup = previous.close < previous_lower and prior_rsi < 30.0
    short_setup = previous.close > previous_upper and prior_rsi > 70.0
    if not long_setup and not short_setup:
        return StrategyResult(None, ReasonCode.NO_STRETCH, indicator_values)

    if long_setup:
        if not (band_low <= current.close <= band_high):
            return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)
        entry = current.close
        stop = min(candle.low for candle in history[-3:]) - 0.25 * current_atr
        stop_distance = entry - stop
        if stop_distance < current_atr:
            stop = entry - current_atr
            stop_distance = entry - stop
        if stop_distance > 3.0 * current_atr:
            return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
        reward = mid - entry
        if reward < stop_distance:
            return StrategyResult(None, ReasonCode.REWARD_TOO_SMALL, indicator_values)
        proposal = Proposal(Side.LONG, entry, stop, mid, "STRATEGY_B", indicator_values)
        return StrategyResult(proposal, None, indicator_values)

    if not (band_low <= current.close <= band_high):
        return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)
    entry = current.close
    stop = max(candle.high for candle in history[-3:]) + 0.25 * current_atr
    stop_distance = stop - entry
    if stop_distance < current_atr:
        stop = entry + current_atr
        stop_distance = stop - entry
    if stop_distance > 3.0 * current_atr:
        return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
    reward = entry - mid
    if reward < stop_distance:
        return StrategyResult(None, ReasonCode.REWARD_TOO_SMALL, indicator_values)
    proposal = Proposal(Side.SHORT, entry, stop, mid, "STRATEGY_B", indicator_values)
    return StrategyResult(proposal, None, indicator_values)
