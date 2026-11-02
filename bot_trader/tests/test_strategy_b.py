from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from bot.core.strategy_b import propose
from bot.core.types import Candle, ReasonCode, Side


def make_history(length: int = 300) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(length):
        value = 100.0 + (index % 7) * 0.01
        candles.append(
            Candle(
                time=start + timedelta(hours=index),
                open=value,
                high=value + 0.6,
                low=value - 0.6,
                close=value,
            )
        )
    return candles


def test_strategy_b_rejects_non_h1_timeframe() -> None:
    with pytest.raises(ValueError, match="H1"):
        propose(make_history(), timeframe="M15")


def test_strategy_b_valid_long_trade_has_correct_stop_target_order() -> None:
    history = make_history()
    history[-2] = Candle(
        time=history[-2].time,
        open=96.0,
        high=98.0,
        low=93.0,
        close=94.0,
    )
    history[-1] = Candle(
        time=history[-1].time,
        open=100.0,
        high=101.0,
        low=98.0,
        close=100.0,
    )
    with patch("bot.core.strategy_b.bollinger") as bollinger_mock, patch(
        "bot.core.strategy_b.rsi"
    ) as rsi_mock, patch("bot.core.strategy_b.adx") as adx_mock, patch(
        "bot.core.strategy_b.atr"
    ) as atr_mock:
        middle = [100.0] * len(history)
        middle[-1] = 110.0
        upper = [105.0] * len(history)
        upper[-1] = 103.0
        upper[-2] = 104.0
        lower = [95.0] * len(history)
        lower[-1] = 97.0
        lower[-2] = 95.0
        bollinger_mock.return_value = (middle, upper, lower)
        rsi_values = [50.0] * len(history)
        rsi_values[-2] = 25.0
        rsi_mock.return_value = rsi_values
        adx_values = [10.0] * len(history)
        adx_mock.return_value = adx_values
        atr_values = [10.0] * len(history)
        atr_mock.return_value = atr_values

        result = propose(history)

    assert result.proposal is not None
    assert result.proposal.side is Side.LONG
    assert result.proposal.stop_loss < result.proposal.entry < result.proposal.take_profit
    assert result.reason is None


def test_strategy_b_rejects_the_expected_skip_reasons() -> None:
    def build_history(close_previous: float, close_current: float, low_current: float, prev_lower: float):
        history = make_history()
        history[-2] = Candle(
            time=history[-2].time,
            open=close_previous,
            high=close_previous + 2.0,
            low=close_previous - 1.0,
            close=close_previous,
        )
        history[-1] = Candle(
            time=history[-1].time,
            open=close_current,
            high=close_current + 1.0,
            low=low_current,
            close=close_current,
        )
        return history

    cases = [
        (
            "NO_QUIET_MARKET",
            {
                "close_previous": 90.0,
                "close_current": 100.0,
                "low_current": 98.0,
                "prev_lower": 95.0,
                "adx_value": 25.0,
                "prior_rsi": 25.0,
                "upper": 103.0,
                "lower": 97.0,
            },
        ),
        (
            "NO_STRETCH",
            {
                "close_previous": 90.0,
                "close_current": 100.0,
                "low_current": 98.0,
                "prev_lower": 95.0,
                "adx_value": 10.0,
                "prior_rsi": 35.0,
                "upper": 103.0,
                "lower": 97.0,
            },
        ),
        (
            "NO_TRIGGER",
            {
                "close_previous": 90.0,
                "close_current": 110.0,
                "low_current": 108.0,
                "prev_lower": 95.0,
                "adx_value": 10.0,
                "prior_rsi": 25.0,
                "upper": 99.0,
                "lower": 97.0,
            },
        ),
        (
            "STOP_TOO_WIDE",
            {
                "close_previous": 88.0,
                "close_current": 100.0,
                "low_current": 98.0,
                "prev_lower": 95.0,
                "adx_value": 10.0,
                "prior_rsi": 25.0,
                "upper": 103.0,
                "lower": 92.0,
            },
        ),
        (
            "REWARD_TOO_SMALL",
            {
                "close_previous": 90.0,
                "close_current": 100.0,
                "low_current": 98.0,
                "prev_lower": 95.0,
                "adx_value": 10.0,
                "prior_rsi": 25.0,
                "upper": 103.0,
                "lower": 97.0,
            },
        ),
    ]

    for expected_reason, kwargs in cases:
        history = build_history(
            kwargs["close_previous"],
            kwargs["close_current"],
            kwargs["low_current"],
            kwargs["prev_lower"],
        )
        with patch("bot.core.strategy_b.bollinger") as bollinger_mock, patch(
            "bot.core.strategy_b.rsi"
        ) as rsi_mock, patch("bot.core.strategy_b.adx") as adx_mock, patch(
            "bot.core.strategy_b.atr"
        ) as atr_mock:
            middle = [100.0] * len(history)
            middle[-1] = 100.0 if expected_reason == "REWARD_TOO_SMALL" else 102.5
            upper_values = [105.0] * len(history)
            upper_values[-1] = kwargs["upper"]
            upper_values[-2] = kwargs["upper"] + 1.0
            lower_values = [95.0] * len(history)
            lower_values[-1] = kwargs["lower"]
            lower_values[-2] = kwargs["prev_lower"]
            bollinger_mock.return_value = (middle, upper_values, lower_values)
            rsi_values = [50.0] * len(history)
            rsi_values[-2] = kwargs["prior_rsi"]
            rsi_mock.return_value = rsi_values
            adx_values = [10.0] * len(history)
            adx_values[-1] = kwargs["adx_value"]
            adx_mock.return_value = adx_values
            atr_values = [10.0 if expected_reason == "REWARD_TOO_SMALL" else 1.0] * len(history)
            atr_mock.return_value = atr_values
            result = propose(history)
            if expected_reason == "REWARD_TOO_SMALL":
                assert result.reason is ReasonCode.REWARD_TOO_SMALL
            else:
                assert result.reason is getattr(ReasonCode, expected_reason)
