from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from bot.core.indicators import adx, atr, bollinger, ema, rsi, true_ranges
from bot.core.types import Candle


def test_ema_matches_pandas_adjust_false_reference() -> None:
    values = [1.2, 1.4, 1.1, 1.8, 1.7, 2.0]
    reference = pd.Series(values).ewm(span=3, adjust=False).mean().tolist()

    assert ema(values, 3) == pytest.approx(reference)


def test_atr_true_range_and_wilder_smoothing_match_pandas_reference() -> None:
    candles = [
        Candle(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index), *prices)
        for index, prices in enumerate(
            (
                (11.0, 12.0, 10.0, 11.0),
                (12.0, 13.0, 11.0, 12.0),
                (13.0, 14.0, 12.0, 13.0),
                (11.0, 14.0, 10.0, 11.0),
            )
        )
    ]
    expected_ranges = [2.0, 2.0, 2.0, 4.0]
    reference = (
        pd.Series(expected_ranges)
        .ewm(alpha=1 / 3, adjust=False)
        .mean()
        .tolist()
    )

    assert true_ranges(candles) == pytest.approx(expected_ranges)
    assert atr(candles, 3) == pytest.approx(reference)


def test_atr_rejects_invalid_period_and_handles_empty_history() -> None:
    assert atr([], 14) == []
    with pytest.raises(ValueError, match="period"):
        atr([], 0)


# --------------------------------------------------------------------------------------------
# Strategy B indicators: Bollinger Bands, RSI and ADX against reference values.
#
# The series below is 1,000 random-walk candles from a fixed seed. The golden numbers were
# produced by TA-Lib 0.8.3 (the library the design record names for reference values) and are
# stored here, so these tests run without TA-Lib. The ``*_matches_ta_lib`` tests compare the whole
# series when TA-Lib happens to be installed and are skipped otherwise.
# --------------------------------------------------------------------------------------------


def random_walk_candles(count: int = 1_000, seed: int = 42) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    start = datetime(2026, 1, 5, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(count):
        open_price = price
        close = open_price + rng.gauss(0, 0.4)
        high = max(open_price, close) + abs(rng.gauss(0, 0.2))
        low = min(open_price, close) - abs(rng.gauss(0, 0.2))
        candles.append(Candle(start + timedelta(hours=index), open_price, high, low, close))
        price = close
    return candles


def test_bollinger_uses_the_population_standard_deviation() -> None:
    closes = [candle.close for candle in random_walk_candles()]
    series = pd.Series(closes)
    mean = series.rolling(20).mean()
    population_sd = series.rolling(20).std(ddof=0)
    sample_sd = series.rolling(20).std(ddof=1)

    middle, upper, lower = bollinger(closes, 20, 2.0)

    assert middle[:19] == [None] * 19
    assert middle[19:] == pytest.approx(mean[19:].tolist())
    assert upper[19:] == pytest.approx((mean + 2.0 * population_sd)[19:].tolist())
    assert lower[19:] == pytest.approx((mean - 2.0 * population_sd)[19:].tolist())
    assert upper[-1] != pytest.approx((mean + 2.0 * sample_sd).iloc[-1])  # sample SD is wrong


def test_indicators_match_values_checked_against_ta_lib() -> None:
    candles = random_walk_candles()
    closes = [candle.close for candle in candles]

    _, upper, lower = bollinger(closes, 20, 2.0)
    middle = bollinger(closes, 20, 2.0)[0]
    rsi_values = rsi(closes, 14)

    assert rsi_values[-1] == pytest.approx(54.255492133259196, abs=1e-9)
    assert rsi_values[-2] == pytest.approx(50.43180094657892, abs=1e-9)
    assert adx(candles, 14)[-1] == pytest.approx(12.451660230047723, abs=1e-9)
    assert upper[-1] == pytest.approx(101.52167027018018, abs=1e-9)
    assert middle[-1] == pytest.approx(100.60924148430087, abs=1e-9)
    assert lower[-1] == pytest.approx(99.69681269842155, abs=1e-9)
    assert atr(candles, 14)[-1] == pytest.approx(0.7273919506602241, abs=1e-9)


def test_rsi_adx_bollinger_and_atr_match_ta_lib_over_the_whole_series() -> None:
    talib = pytest.importorskip("talib")
    np = pytest.importorskip("numpy")
    candles = random_walk_candles()
    close = np.array([c.close for c in candles])
    high = np.array([c.high for c in candles])
    low = np.array([c.low for c in candles])

    rsi_reference = talib.RSI(close, 14)
    assert rsi(list(close), 14)[14:] == pytest.approx(rsi_reference[14:].tolist(), abs=1e-9)

    adx_reference = talib.ADX(high, low, close, 14)
    # the first ADX values differ slightly because the seed window is defined one candle apart;
    # the difference dies out within a few hundred candles, long before a 1,000-candle window ends
    assert adx(candles, 14)[300:] == pytest.approx(adx_reference[300:].tolist(), abs=1e-9)

    upper_ref, middle_ref, lower_ref = talib.BBANDS(close, 20, 2.0, 2.0, 0)
    middle, upper, lower = bollinger(list(close), 20, 2.0)
    assert upper[19:] == pytest.approx(upper_ref[19:].tolist(), abs=1e-9)
    assert middle[19:] == pytest.approx(middle_ref[19:].tolist(), abs=1e-9)
    assert lower[19:] == pytest.approx(lower_ref[19:].tolist(), abs=1e-9)

    assert atr(candles, 14)[300:] == pytest.approx(
        talib.ATR(high, low, close, 14)[300:].tolist(), abs=1e-9
    )


def mirrored(candles: list[Candle]) -> list[Candle]:
    return [
        Candle(c.time, 200.0 - c.open, 200.0 - c.low, 200.0 - c.high, 200.0 - c.close)
        for c in candles
    ]


def test_rsi_and_adx_are_bounded_and_symmetric_under_price_reflection() -> None:
    candles = random_walk_candles()
    mirror = mirrored(candles)
    closes = [c.close for c in candles]
    mirror_closes = [c.close for c in mirror]

    rsi_values = rsi(closes, 14)
    rsi_mirror = rsi(mirror_closes, 14)
    adx_values = adx(candles, 14)
    adx_mirror = adx(mirror, 14)

    assert all(0.0 <= v <= 100.0 for v in rsi_values if v is not None)
    assert all(0.0 <= v <= 100.0 for v in adx_values if v is not None)
    assert [100.0 - v for v in rsi_values[14:]] == pytest.approx(rsi_mirror[14:])
    assert adx_values[27:] == pytest.approx(adx_mirror[27:])


def test_rsi_and_adx_return_none_until_they_have_enough_candles() -> None:
    candles = random_walk_candles(60)
    closes = [c.close for c in candles]

    assert rsi(closes[:14], 14) == [None] * 14
    assert rsi(closes[:15], 14)[14] is not None
    assert adx(candles[:27], 14) == [None] * 27
    assert adx(candles[:28], 14)[26] is not None


def test_rsi_extremes_and_flat_market() -> None:
    rising = [100.0 + index for index in range(30)]
    falling = [100.0 - index for index in range(30)]
    flat = [100.0] * 30

    assert rsi(rising, 14)[-1] == pytest.approx(100.0)
    assert rsi(falling, 14)[-1] == pytest.approx(0.0)
    assert rsi(flat, 14)[-1] == pytest.approx(50.0)


def test_adx_is_high_in_a_steady_trend_and_low_in_a_choppy_market() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    trend = [
        Candle(start + timedelta(hours=i), 100 + i, 100.5 + i, 99.5 + i, 100.2 + i)
        for i in range(120)
    ]
    chop = [
        Candle(start + timedelta(hours=i), 100, 100.5, 99.5, 100.2 if i % 2 else 99.8)
        for i in range(120)
    ]

    assert adx(trend, 14)[-1] > 50.0
    assert adx(chop, 14)[-1] < 10.0

