from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from bot.core.indicators import adx, atr, bollinger, rsi
from bot.core.types import Candle, Proposal, ReasonCode, Side, StrategyResult


@dataclass(frozen=True, slots=True)
class StrategyBParams:
    bollinger_period: int = 20
    bollinger_deviations: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    adx_period: int = 14
    adx_quiet_max: float = 20.0
    atr_period: int = 14
    stop_lookback: int = 3
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    min_reward_r: float = 1.0
    time_stop_candles: int = 24
    cooldown_bars_after_stop: int = 6
    min_candles: int = 300
    window_candles: int = 1_000

    def __post_init__(self) -> None:
        integer_values = (
            self.bollinger_period,
            self.rsi_period,
            self.adx_period,
            self.atr_period,
            self.stop_lookback,
            self.time_stop_candles,
            self.cooldown_bars_after_stop,
            self.min_candles,
            self.window_candles,
        )
        if any(value < 1 for value in integer_values):
            raise ValueError("Strategy B integer parameters must be positive")
        float_values = (
            self.bollinger_deviations,
            self.rsi_oversold,
            self.rsi_overbought,
            self.adx_quiet_max,
            self.stop_buffer_atr,
            self.min_stop_atr,
            self.max_stop_atr,
            self.min_reward_r,
        )
        if any(not isfinite(value) for value in float_values):
            raise ValueError("Strategy B numeric parameters must be finite")
        if self.bollinger_deviations < 0:
            raise ValueError("Strategy B Bollinger deviation cannot be negative")
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError("Strategy B RSI thresholds must be ordered")
        if self.adx_quiet_max < 0:
            raise ValueError("Strategy B ADX quiet-max cannot be negative")
        if self.stop_buffer_atr < 0 or self.min_stop_atr <= 0 or self.max_stop_atr < self.min_stop_atr:
            raise ValueError("Strategy B stop parameters are invalid")
        if self.min_reward_r <= 0:
            raise ValueError("Strategy B minimum reward ratio must be positive")
        if self.window_candles < self.min_candles:
            raise ValueError("Strategy B window must be at least the warm-up length")


def propose(
    candles: Sequence[Candle],
    params: StrategyBParams | None = None,
    *,
    timeframe: str = "H1",
) -> StrategyResult:
    """Evaluate the fixed H1 quiet-market mean-reversion strategy."""
    if timeframe != "H1":
        raise ValueError("Strategy B is defined for H1 candles only")
    active_params = StrategyBParams() if params is None else params
    history = list(candles[-active_params.window_candles :])
    if len(history) < active_params.min_candles:
        return StrategyResult(None, ReasonCode.WARMUP)

    closes = [candle.close for candle in history]
    middle, upper, lower = bollinger(
        closes,
        active_params.bollinger_period,
        active_params.bollinger_deviations,
    )
    rsi14 = rsi(closes, active_params.rsi_period)
    adx14 = adx(history, active_params.adx_period)
    atr14 = atr(history, active_params.atr_period)

    if any(
        value is None
        for value in (
            middle[-1],
            lower[-1],
            upper[-1],
            rsi14[-2],
            adx14[-1],
            atr14[-1],
        )
    ):
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
    if current_adx >= active_params.adx_quiet_max:
        return StrategyResult(None, ReasonCode.NO_QUIET_MARKET, indicator_values)

    previous = history[-2]
    current = history[-1]
    previous_lower = lower[-2]
    previous_upper = upper[-2]
    if previous_lower is None or previous_upper is None:
        return StrategyResult(None, ReasonCode.WARMUP, indicator_values)

    long_setup = previous.close < previous_lower and prior_rsi < active_params.rsi_oversold
    short_setup = previous.close > previous_upper and prior_rsi > active_params.rsi_overbought
    if not long_setup and not short_setup:
        return StrategyResult(None, ReasonCode.NO_STRETCH, indicator_values)

    if long_setup:
        if not (band_low <= current.close <= band_high):
            return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)
        entry = current.close
        stop = min(candle.low for candle in history[-active_params.stop_lookback :]) - (
            active_params.stop_buffer_atr * current_atr
        )
        stop_distance = entry - stop
        if stop_distance < active_params.min_stop_atr * current_atr:
            stop = entry - active_params.min_stop_atr * current_atr
            stop_distance = entry - stop
        if stop_distance > active_params.max_stop_atr * current_atr:
            return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
        reward = mid - entry
        if reward < active_params.min_reward_r * stop_distance:
            return StrategyResult(None, ReasonCode.REWARD_TOO_SMALL, indicator_values)
        proposal = Proposal(Side.LONG, entry, stop, mid, "STRATEGY_B", indicator_values)
        return StrategyResult(proposal, None, indicator_values)

    if not (band_low <= current.close <= band_high):
        return StrategyResult(None, ReasonCode.NO_TRIGGER, indicator_values)
    entry = current.close
    stop = max(candle.high for candle in history[-active_params.stop_lookback :]) + (
        active_params.stop_buffer_atr * current_atr
    )
    stop_distance = stop - entry
    if stop_distance < active_params.min_stop_atr * current_atr:
        stop = entry + active_params.min_stop_atr * current_atr
        stop_distance = stop - entry
    if stop_distance > active_params.max_stop_atr * current_atr:
        return StrategyResult(None, ReasonCode.STOP_TOO_WIDE, indicator_values)
    reward = entry - mid
    if reward < active_params.min_reward_r * stop_distance:
        return StrategyResult(None, ReasonCode.REWARD_TOO_SMALL, indicator_values)
    proposal = Proposal(Side.SHORT, entry, stop, mid, "STRATEGY_B", indicator_values)
    return StrategyResult(proposal, None, indicator_values)
