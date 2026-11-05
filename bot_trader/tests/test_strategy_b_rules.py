"""Strategy B rule tests on real candles (no mocked indicators).

Every fixture is built from plain numbers, so each rule can be checked by hand:

* ``flat_closes`` is a quiet, choppy market around 100 (fixed 14-step pattern, no randomness).
* ``long_setup`` takes that market and adds five straight down candles (a stretch below the lower
  Bollinger Band with RSI under 30) followed by a snap-back candle that closes inside the band.
* ``reflect`` mirrors a series around 100, which turns every long fixture into the matching short
  fixture, so each rule is tested on both sides with the same numbers.

The expected stop and target are recomputed from the indicator functions, which have their own
reference tests in ``test_indicators.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bot.core.indicators import adx, atr, bollinger, rsi
from bot.core.strategy_b import StrategyBParams, propose
from bot.core.types import Candle, ReasonCode, Side

START = datetime(2026, 1, 5, tzinfo=UTC)
WICK = 0.01
CHOP = (0, 3, 1, -2, -3, 0, 2, -1, -3, 1, 3, -1, -2, 2)
LENGTH = 330


# ----------------------------------------------------------------------------- fixtures


def candles_from_closes(closes: list[float], wick: float = WICK) -> list[Candle]:
    """Each candle opens at the previous close and has a small wick on both sides."""
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                time=START + timedelta(hours=index),
                open=previous,
                high=max(previous, close) + wick,
                low=min(previous, close) - wick,
                close=close,
            )
        )
        previous = close
    return candles


def flat_closes(length: int = LENGTH, unit: float = 0.04) -> list[float]:
    return [100.0 + unit * CHOP[index % len(CHOP)] for index in range(length)]


def long_setup_closes(
    *, down_candles: int = 5, step: float = 0.22, snap: float = 1.5, length: int = LENGTH
) -> list[float]:
    closes = flat_closes(length - down_candles - 1)
    current = closes[-1]
    for _ in range(down_candles):
        current -= step
        closes.append(current)
    closes.append(current + snap * step)
    return closes


def long_setup() -> list[Candle]:
    return candles_from_closes(long_setup_closes())


def reflect(candles: list[Candle]) -> list[Candle]:
    """Mirror prices around 100: a long setup becomes the matching short setup."""
    return [
        Candle(
            time=candle.time,
            open=200.0 - candle.open,
            high=200.0 - candle.low,
            low=200.0 - candle.high,
            close=200.0 - candle.close,
        )
        for candle in candles
    ]


def metrics(candles: list[Candle]) -> dict[str, float]:
    closes = [candle.close for candle in candles]
    middle, upper, lower = bollinger(closes, 20, 2.0)
    rsi_values = rsi(closes, 14)
    adx_values = adx(candles, 14)
    atr_values = atr(candles, 14)
    return {
        "mid": middle[-1],
        "upper_now": upper[-1],
        "lower_now": lower[-1],
        "upper_prev": upper[-2],
        "lower_prev": lower[-2],
        "rsi_prev": rsi_values[-2],
        "adx": adx_values[-1],
        "atr": atr_values[-1],
    }


SIDES = [
    pytest.param(lambda candles: candles, Side.LONG, id="long"),
    pytest.param(reflect, Side.SHORT, id="short"),
]


# ----------------------------------------------------------------------------- sanity


def test_fixture_matches_the_hand_described_setup() -> None:
    """If an indicator change breaks the fixture, this fails first and says why."""
    candles = long_setup()
    values = metrics(candles)

    assert values["adx"] < 20.0, "market must be quiet"
    assert values["rsi_prev"] < 30.0, "previous candle must be oversold"
    assert candles[-2].close < values["lower_prev"], "previous candle closed below the lower band"
    assert values["lower_now"] <= candles[-1].close <= values["upper_now"], "back inside the band"
    assert candles[-1].close < values["mid"], "target (middle band) is above the entry"


# ----------------------------------------------------------------------------- a valid trade


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_valid_setup_returns_exact_entry_stop_and_target(transform, side: Side) -> None:
    candles = transform(long_setup())
    values = metrics(candles)

    result = propose(candles)

    assert result.reason is None
    proposal = result.proposal
    assert proposal is not None
    assert proposal.side is side
    assert proposal.reason == "STRATEGY_B"
    assert proposal.entry == candles[-1].close
    assert proposal.take_profit == pytest.approx(values["mid"])
    if side is Side.LONG:
        expected_stop = min(c.low for c in candles[-3:]) - 0.25 * values["atr"]
        assert proposal.stop_loss == pytest.approx(expected_stop)
        assert proposal.stop_loss < proposal.entry < proposal.take_profit
    else:
        expected_stop = max(c.high for c in candles[-3:]) + 0.25 * values["atr"]
        assert proposal.stop_loss == pytest.approx(expected_stop)
        assert proposal.take_profit < proposal.entry < proposal.stop_loss
    distance = abs(proposal.entry - proposal.stop_loss)
    assert values["atr"] <= distance <= 3.0 * values["atr"]
    assert abs(proposal.take_profit - proposal.entry) >= distance


def test_short_setup_is_the_exact_mirror_of_the_long_setup() -> None:
    long_proposal = propose(long_setup()).proposal
    short_proposal = propose(reflect(long_setup())).proposal

    assert long_proposal is not None and short_proposal is not None
    assert short_proposal.entry == pytest.approx(200.0 - long_proposal.entry)
    assert short_proposal.stop_loss == pytest.approx(200.0 - long_proposal.stop_loss)
    assert short_proposal.take_profit == pytest.approx(200.0 - long_proposal.take_profit)


def test_decision_logs_every_indicator_value_with_the_proposal() -> None:
    result = propose(long_setup())

    assert result.proposal is not None
    assert [name for name, _ in result.indicators] == [
        "bb_middle",
        "bb_lower",
        "bb_upper",
        "rsi14_previous",
        "adx14",
        "atr14",
    ]
    assert result.proposal.indicators == result.indicators


def test_decision_is_the_same_when_all_prices_are_scaled() -> None:
    """Rules are ATR and band based, so the price level must not matter."""
    scaled = [
        replace(
            candle,
            open=candle.open * 1.1,
            high=candle.high * 1.1,
            low=candle.low * 1.1,
            close=candle.close * 1.1,
        )
        for candle in long_setup()
    ]
    original = propose(long_setup()).proposal
    result = propose(scaled).proposal

    assert original is not None and result is not None
    assert result.side is original.side
    assert result.entry == pytest.approx(original.entry * 1.1)


# ----------------------------------------------------------------------------- WARMUP


def test_fewer_than_300_closed_candles_is_warmup_and_exactly_300_is_allowed() -> None:
    candles = long_setup()

    assert propose(candles[-299:]).reason is ReasonCode.WARMUP
    assert propose(candles[-300:]).proposal is not None


def test_candles_older_than_the_1000_candle_window_do_not_change_the_decision() -> None:
    candles = long_setup()
    older_closes = [100.0 + 0.04 * CHOP[i % len(CHOP)] for i in range(700)]
    older_closes_changed = [100.0 + 9.0 * CHOP[i % len(CHOP)] for i in range(20)] + older_closes[20:]

    def older(closes: list[float]) -> list[Candle]:
        wild = candles_from_closes(closes)
        return [replace(c, time=START - timedelta(hours=700 - i)) for i, c in enumerate(wild)]

    total = len(older(older_closes)) + len(candles)  # 1,030 candles: the first 30 are outside
    assert total > 1_000
    assert propose(older(older_closes) + candles) == propose(older(older_closes_changed) + candles)


# ----------------------------------------------------------------------------- NO_QUIET_MARKET


@pytest.mark.parametrize(("direction", "label"), [(1, "uptrend"), (-1, "downtrend")])
def test_strong_trend_is_not_a_quiet_market(direction: int, label: str) -> None:
    closes = [100.0 + direction * 0.05 * index for index in range(LENGTH)]
    candles = candles_from_closes(closes)

    assert metrics(candles)["adx"] >= 20.0, label
    assert propose(candles).reason is ReasonCode.NO_QUIET_MARKET


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_adx_exactly_at_the_limit_is_not_quiet_but_just_below_is(transform, side: Side) -> None:
    candles = transform(long_setup())
    adx_now = metrics(candles)["adx"]

    at_limit = propose(candles, StrategyBParams(adx_quiet_max=adx_now))
    just_above = propose(candles, StrategyBParams(adx_quiet_max=adx_now + 1e-9))

    assert at_limit.reason is ReasonCode.NO_QUIET_MARKET  # rule: ADX below the limit
    assert just_above.proposal is not None and just_above.proposal.side is side


# ----------------------------------------------------------------------------- NO_STRETCH


def test_flat_market_has_no_stretch() -> None:
    candles = candles_from_closes(flat_closes())

    assert propose(candles).reason is ReasonCode.NO_STRETCH


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_rsi_exactly_at_the_threshold_is_not_a_stretch(transform, side: Side) -> None:
    candles = transform(long_setup())
    rsi_prev = metrics(candles)["rsi_prev"]
    if side is Side.LONG:
        at_limit = StrategyBParams(rsi_oversold=rsi_prev)
        beyond = StrategyBParams(rsi_oversold=rsi_prev + 1e-9)
    else:
        at_limit = StrategyBParams(rsi_overbought=rsi_prev)
        beyond = StrategyBParams(rsi_overbought=rsi_prev - 1e-9)

    assert propose(candles, at_limit).reason is ReasonCode.NO_STRETCH  # rule: strictly beyond
    assert propose(candles, beyond).proposal is not None


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_band_break_without_extreme_rsi_is_not_a_stretch(transform, side: Side) -> None:
    candles = transform(long_setup())
    params = (
        StrategyBParams(rsi_oversold=5.0)
        if side is Side.LONG
        else StrategyBParams(rsi_overbought=95.0)
    )

    assert propose(candles, params).reason is ReasonCode.NO_STRETCH


# ----------------------------------------------------------------------------- NO_TRIGGER


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_still_outside_the_band_on_the_signal_candle_is_no_trigger(transform, side: Side) -> None:
    closes = long_setup_closes()
    closes[-1] = closes[-2] - 0.02  # no snap-back: the last close is still below the lower band
    long_candles = candles_from_closes(closes)
    values = metrics(long_candles)
    # the stretch itself is valid, only the trigger is missing
    assert values["adx"] < 20.0
    assert values["rsi_prev"] < 30.0
    assert long_candles[-2].close < values["lower_prev"]
    assert long_candles[-1].close < values["lower_now"]

    assert propose(transform(long_candles)).reason is ReasonCode.NO_TRIGGER


# ----------------------------------------------------------------------------- STOP_TOO_WIDE


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_stop_wider_than_the_maximum_is_skipped(transform, side: Side) -> None:
    candles = transform(long_setup())
    proposal = propose(candles).proposal
    assert proposal is not None
    distance_in_atr = abs(proposal.entry - proposal.stop_loss) / metrics(candles)["atr"]

    too_wide = propose(candles, StrategyBParams(max_stop_atr=distance_in_atr - 0.01))
    allowed = propose(candles, StrategyBParams(max_stop_atr=distance_in_atr + 0.01))

    assert too_wide.reason is ReasonCode.STOP_TOO_WIDE
    assert allowed.proposal is not None


# ----------------------------------------------------------------------------- stop lookback


def with_extreme(candles: list[Candle], index: int, amount: float) -> list[Candle]:
    """Push one candle's low further down by ``amount`` (its high, open and close are unchanged)."""
    changed = list(candles)
    changed[index] = replace(changed[index], low=changed[index].low - amount)
    return changed


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_stop_uses_exactly_the_last_three_candles_including_the_signal_candle(
    transform, side: Side
) -> None:
    # a deeper low three candles back is used; the same deeper low four candles back is not
    for index, inside_window in ((-3, True), (-4, False)):
        long_candles = with_extreme(long_setup(), index, 0.45)
        candles = transform(long_candles)
        values = metrics(long_candles)
        lows = [c.low for c in long_candles]
        window_stop = min(lows[-3:]) - 0.25 * values["atr"]
        wider_stop = min(lows[-4:]) - 0.25 * values["atr"]
        deepest = min(lows[-4:])
        assert (lows[-3:].count(deepest) == 1) is inside_window  # the deep low is in the window
        assert (window_stop == pytest.approx(wider_stop)) is inside_window

        # a deep wick also raises ADX and the stop width, so those limits are relaxed for this test
        relaxed = StrategyBParams(adx_quiet_max=60.0, max_stop_atr=20.0, min_reward_r=0.01)
        proposal = propose(candles, relaxed).proposal

        assert proposal is not None, "the stop tweak must not change any other rule"
        expected = window_stop if side is Side.LONG else 200.0 - window_stop
        assert proposal.stop_loss == pytest.approx(expected)


# ----------------------------------------------------------------------------- stop widening


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_a_stop_closer_than_the_minimum_is_widened_to_exactly_the_minimum(
    transform, side: Side
) -> None:
    candles = transform(long_setup())
    atr_now = metrics(candles)["atr"]
    params = StrategyBParams(min_stop_atr=2.5, max_stop_atr=3.0)

    proposal = propose(candles, params).proposal

    assert proposal is not None
    assert abs(proposal.entry - proposal.stop_loss) == pytest.approx(2.5 * atr_now)
    if side is Side.LONG:
        assert proposal.stop_loss == pytest.approx(proposal.entry - 2.5 * atr_now)
    else:
        assert proposal.stop_loss == pytest.approx(proposal.entry + 2.5 * atr_now)


# ----------------------------------------------------------------------------- REWARD_TOO_SMALL


@pytest.mark.parametrize(("transform", "side"), SIDES)
def test_reward_below_the_minimum_multiple_of_the_stop_is_skipped(transform, side: Side) -> None:
    candles = transform(long_setup())
    proposal = propose(candles).proposal
    assert proposal is not None
    ratio = abs(proposal.take_profit - proposal.entry) / abs(proposal.entry - proposal.stop_loss)

    assert propose(candles, StrategyBParams(min_reward_r=ratio * 1.001)).reason is (
        ReasonCode.REWARD_TOO_SMALL
    )
    assert propose(candles, StrategyBParams(min_reward_r=ratio * 0.999)).proposal is not None


# ----------------------------------------------------------------------------- purity


def test_no_look_ahead_decision_at_candle_t_ignores_later_candles() -> None:
    full = long_setup()
    garbage = candles_from_closes([100.0 + 7.0 * CHOP[i % len(CHOP)] for i in range(40)])
    garbage = [replace(c, time=full[-1].time + timedelta(hours=i + 1)) for i, c in enumerate(garbage)]

    for end in range(len(full) - 8, len(full) + 1):
        clean = propose(full[:end])
        with_future = propose((full + garbage)[:end])
        assert clean == with_future


def test_propose_does_not_modify_its_input() -> None:
    candles = long_setup()
    snapshot = list(candles)

    propose(candles)

    assert candles == snapshot


def test_same_input_gives_identical_output() -> None:
    assert propose(long_setup()) == propose(long_setup())


# ----------------------------------------------------------------------------- parameters


def test_strategy_b_params_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="RSI"):
        StrategyBParams(rsi_oversold=70.0, rsi_overbought=30.0)
    with pytest.raises(ValueError, match="window"):
        StrategyBParams(min_candles=500, window_candles=400)
    with pytest.raises(ValueError, match="positive"):
        StrategyBParams(stop_lookback=0)
    with pytest.raises(ValueError, match="finite"):
        StrategyBParams(adx_quiet_max=float("nan"))
    with pytest.raises(ValueError, match="stop"):
        StrategyBParams(min_stop_atr=2.0, max_stop_atr=1.0)
    with pytest.raises(ValueError, match="reward"):
        StrategyBParams(min_reward_r=0.0)
