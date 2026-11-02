from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from bot.core.indicators import atr, ema, true_ranges
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
