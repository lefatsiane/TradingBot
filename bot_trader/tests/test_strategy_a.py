from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bot.core import strategy_a
from bot.core.strategy_a import StrategyAParams
from bot.core.types import Candle, ReasonCode, Side

PARAMS = StrategyAParams()
HAND_BUILT_PARAMS = StrategyAParams(
    ema_fast=2,
    ema_mid=3,
    ema_slow=4,
    atr_period=2,
    slope_lookback=2,
    pullback_window=2,
    stop_lookback=2,
    stop_buffer_atr=0.1,
    min_stop_atr=0.1,
    max_stop_atr=5.0,
    min_candles=10,
    window_candles=10,
)


def hand_built_fixture(
    side: Side, timeframe_hours: int
) -> list[Candle]:
    closes = (
        [99, 99.5, 100, 100.5, 101, 102, 103, 102.5, 102.8, 104]
        if side is Side.LONG
        else [101, 100.5, 100, 99.5, 99, 98, 97, 97.5, 97.2, 96]
    )
    direction = 1 if side is Side.LONG else -1
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            time=start + timedelta(hours=index * timeframe_hours),
            open=close - direction * 0.05,
            high=close + 0.2,
            low=close - 0.2,
            close=close,
        )
        for index, close in enumerate(closes)
    ]


def fixture_for(
    side: Side,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Candle], dict[int, list[float]]]:
    candles: list[Candle] = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(PARAMS.min_candles - 1):
        high = 101.0 if side is Side.LONG else 100.5
        candles.append(
            Candle(start + timedelta(hours=index), 100.0, high, 99.5, 100.0)
        )
    if side is Side.LONG:
        current = Candle(
            start + timedelta(hours=PARAMS.min_candles - 1),
            101.7,
            102.5,
            101.5,
            102.0,
        )
        ema_values = {
            20: [99.5] * PARAMS.min_candles,
            50: [99.0] * PARAMS.min_candles,
            200: [98.0] * PARAMS.min_candles,
        }
        ema_values[200][-PARAMS.slope_lookback - 1] = 97.0
    else:
        current = Candle(
            start + timedelta(hours=PARAMS.min_candles - 1),
            99.0,
            99.5,
            97.5,
            98.0,
        )
        ema_values = {
            20: [100.5] * PARAMS.min_candles,
            50: [101.0] * PARAMS.min_candles,
            200: [102.0] * PARAMS.min_candles,
        }
        ema_values[200][-PARAMS.slope_lookback - 1] = 103.0
    candles.append(current)

    def fake_ema(_: list[float], period: int) -> list[float]:
        return ema_values[period]

    monkeypatch.setattr(strategy_a, "ema", fake_ema)
    monkeypatch.setattr(
        strategy_a,
        "atr",
        lambda values, period: [1.0] * len(values),
    )
    return candles, ema_values


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_strategy_a_returns_exact_entry_stop_and_two_r_target(
    side: Side, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles, _ = fixture_for(side, monkeypatch)

    result = strategy_a.propose(candles, PARAMS)

    assert result.proposal is not None
    proposal = result.proposal
    assert proposal.side is side
    assert proposal.entry == candles[-1].close
    risk = (
        proposal.entry - proposal.stop_loss
        if side is Side.LONG
        else proposal.stop_loss - proposal.entry
    )
    reward = (
        proposal.take_profit - proposal.entry
        if side is Side.LONG
        else proposal.entry - proposal.take_profit
    )
    assert risk == pytest.approx(2.75)
    assert reward == pytest.approx(2 * risk)


@pytest.mark.parametrize(
    ("side", "expected_entry", "expected_stop", "expected_target"),
    [
        (Side.LONG, 104.0, 102.49537109375, 107.0092578125),
        (Side.SHORT, 96.0, 97.50462890625, 92.9907421875),
    ],
)
@pytest.mark.parametrize("timeframe_hours", [1, 4])
def test_hand_built_fixture_is_timeframe_independent(
    side: Side,
    expected_entry: float,
    expected_stop: float,
    expected_target: float,
    timeframe_hours: int,
) -> None:
    candles = hand_built_fixture(side, timeframe_hours)

    result = strategy_a.propose(candles, HAND_BUILT_PARAMS)

    assert result.proposal is not None
    assert result.proposal.side is side
    assert result.proposal.entry == pytest.approx(expected_entry)
    assert result.proposal.stop_loss == pytest.approx(expected_stop)
    assert result.proposal.take_profit == pytest.approx(expected_target)


@pytest.mark.parametrize(
    "failure",
    ["close_position", "ema_order", "slope"],
)
def test_each_trend_condition_can_reject_on_its_own(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles, ema_values = fixture_for(Side.LONG, monkeypatch)
    if failure == "close_position":
        ema_values[200][-1] = candles[-1].close
    elif failure == "ema_order":
        ema_values[50][-1] = ema_values[200][-1]
    else:
        ema_values[200][-PARAMS.slope_lookback - 1] = ema_values[200][-1]

    assert strategy_a.propose(candles, PARAMS).reason is ReasonCode.NO_TREND


@pytest.mark.parametrize("failure", ["no_touch", "close_below_mid"])
def test_pullback_conditions_return_no_pullback(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles, _ = fixture_for(Side.LONG, monkeypatch)
    if failure == "no_touch":
        for index in range(-PARAMS.pullback_window - 1, -1):
            candle = candles[index]
            candles[index] = Candle(
                candle.time, candle.open, candle.high, 99.6, candle.close
            )
    else:
        candle = candles[-3]
        candles[-3] = Candle(candle.time, candle.open, candle.high, 98.9, 98.9)

    assert strategy_a.propose(candles, PARAMS).reason is ReasonCode.NO_PULLBACK


@pytest.mark.parametrize("trigger_close", [99.0, 101.0])
def test_trigger_requires_close_above_ema_and_strictly_above_previous_high(
    trigger_close: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles, _ = fixture_for(Side.LONG, monkeypatch)
    current = candles[-1]
    candles[-1] = Candle(
        current.time,
        current.open,
        max(current.high, trigger_close),
        min(current.low, trigger_close),
        trigger_close,
    )

    assert strategy_a.propose(candles, PARAMS).reason is ReasonCode.NO_TRIGGER


def test_stop_is_widened_to_minimum_atr_and_target_uses_that_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles, _ = fixture_for(Side.LONG, monkeypatch)
    params = replace(PARAMS, stop_lookback=1)

    result = strategy_a.propose(candles, params)

    assert result.proposal is not None
    assert result.proposal.entry - result.proposal.stop_loss == pytest.approx(1.0)
    assert result.proposal.take_profit - result.proposal.entry == pytest.approx(2.0)


def test_stop_wider_than_three_atr_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    candles, _ = fixture_for(Side.LONG, monkeypatch)
    candle = candles[-5]
    candles[-5] = Candle(candle.time, candle.open, candle.high, 90.0, candle.close)

    assert strategy_a.propose(candles, PARAMS).reason is ReasonCode.STOP_TOO_WIDE


@pytest.mark.parametrize(
    ("current_fast", "pullback_fast", "current_mid", "pullback_mid", "expected"),
    [
        (95.0, 99.5, 99.0, 99.0, None),
        (99.5, 99.5, 99.0, 100.5, ReasonCode.NO_PULLBACK),
        (99.5, 99.5, 101.0, 99.0, None),
    ],
)
def test_pullback_uses_each_candles_own_ema_values(
    current_fast: float,
    pullback_fast: float,
    current_mid: float,
    pullback_mid: float,
    expected: ReasonCode | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles, ema_values = fixture_for(Side.LONG, monkeypatch)
    for index in range(-PARAMS.pullback_window - 1, -1):
        candle = candles[index]
        candles[index] = Candle(
            candle.time, candle.open, candle.high, 99.6, candle.close
        )
    touch = candles[-3]
    candles[-3] = Candle(touch.time, touch.open, touch.high, 99.5, touch.close)
    ema_values[20][-1] = current_fast
    ema_values[20][-3] = pullback_fast
    ema_values[50][-1] = current_mid
    ema_values[50][-3] = pullback_mid

    result = strategy_a.propose(candles, PARAMS)

    if expected is None:
        assert result.proposal is not None
    else:
        assert result.reason is expected
