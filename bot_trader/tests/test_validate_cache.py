from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.history_gaps import candle_is_in_session
from tools.validate_cache import (
    CacheCandle,
    _candles_from_frame,
    _validate_one_cache,
    _weekly_boundary_failures,
    validate_cache,
)

SESSIONS = BrokerSessionHoursServerTime()


def _candle(at: datetime, *, prices=(1.1, 1.101, 1.099, 1.1)) -> CacheCandle:
    return CacheCandle(at, *prices)


@pytest.mark.parametrize(
    ("times", "label"),
    [
        (
            (datetime(2026, 6, 30, 8, tzinfo=UTC), datetime(2026, 6, 30, 8, tzinfo=UTC)),
            "duplicate times",
        ),
        (
            (datetime(2026, 6, 30, 9, tzinfo=UTC), datetime(2026, 6, 30, 8, tzinfo=UTC)),
            "out-of-order times",
        ),
    ],
)
def test_validator_reports_duplicate_and_out_of_order_times(
    capsys,
    times,
    label,
) -> None:
    candles = tuple(_candle(value) for value in times)

    assert _validate_one_cache(
        candles, "EURUSD", "H1", SESSIONS, (), 0
    )
    assert label in capsys.readouterr().out


def test_validator_reports_weekend_candles_outside_sessions(capsys) -> None:
    candles = (_candle(datetime(2026, 6, 27, 12, tzinfo=UTC)),)

    assert _validate_one_cache(candles, "EURUSD", "H1", SESSIONS, (), 0)
    output = capsys.readouterr().out
    assert "Saturday/Sunday candles outside trading windows" in output
    assert "2026-06-27T12:00:00+00:00" in output


def test_validator_reports_unexpected_gap(capsys) -> None:
    candles = (
        _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
        _candle(datetime(2026, 6, 30, 12, tzinfo=UTC)),
    )

    assert _validate_one_cache(candles, "EURUSD", "H1", SESSIONS, (), 0)
    output = capsys.readouterr().out
    assert "Unexpected gaps: 1" in output
    assert "2026-06-30T09:00:00+00:00" in output
    assert "Tuesday" in output


@pytest.mark.parametrize(
    ("prices", "message"),
    [
        ((1.1, 1.0, 1.2, 1.1), "invalid OHLC values"),
        ((1.1, 1.2, 1.0, 1.3), "invalid OHLC values"),
        ((0.0, 1.2, 1.0, 1.1), "zero or negative prices"),
        ((-0.1, 1.2, -0.2, 1.1), "zero or negative prices"),
    ],
)
def test_validator_reports_ohlc_and_price_failures(capsys, prices, message) -> None:
    candles = (_candle(datetime(2026, 6, 30, 8, tzinfo=UTC), prices=prices),)

    assert _validate_one_cache(candles, "EURUSD", "H1", SESSIONS, (), 0)
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(
    ("week_open", "week_close"),
    [
        (datetime(2026, 1, 4, 22, tzinfo=UTC), datetime(2026, 1, 9, 21, tzinfo=UTC)),
        (datetime(2026, 7, 5, 21, tzinfo=UTC), datetime(2026, 7, 10, 20, tzinfo=UTC)),
        (datetime(2026, 3, 8, 21, tzinfo=UTC), datetime(2026, 3, 13, 20, tzinfo=UTC)),
        (datetime(2026, 11, 1, 22, tzinfo=UTC), datetime(2026, 11, 6, 21, tzinfo=UTC)),
    ],
)
def test_weekly_boundaries_match_each_us_season_and_clock_change(
    week_open: datetime,
    week_close: datetime,
) -> None:
    assert not _weekly_boundary_failures((_candle(week_open), _candle(week_close)))


def test_weekly_boundary_failure_lists_every_bad_week() -> None:
    candles = (
        _candle(datetime(2026, 1, 4, 23, tzinfo=UTC)),
        _candle(datetime(2026, 1, 9, 22, tzinfo=UTC)),
        _candle(datetime(2026, 1, 11, 23, tzinfo=UTC)),
        _candle(datetime(2026, 1, 16, 22, tzinfo=UTC)),
    )

    failures = _weekly_boundary_failures(candles)

    assert len(failures) == 2
    assert "2026-01-04" in failures[0]
    assert "2026-01-11" in failures[1]


@pytest.mark.parametrize(
    ("timestamp", "timeframe", "expected"),
    [
        (datetime(2026, 3, 8, 21, tzinfo=UTC), "H1", True),
        (datetime(2026, 11, 1, 22, tzinfo=UTC), "H1", True),
        (datetime(2026, 7, 5, 20, tzinfo=UTC), "H4", True),
        (datetime(2026, 7, 5, 16, tzinfo=UTC), "H4", False),
    ],
)
def test_session_windows_use_date_aware_dst_conversion(
    timestamp: datetime,
    timeframe: str,
    expected: bool,
) -> None:
    assert candle_is_in_session(_candle(timestamp), timeframe, SESSIONS) is expected


def test_frame_loader_rejects_naive_cached_times() -> None:
    frame = pd.DataFrame(
        {"time": [datetime(2026, 6, 30, 8)], "open": [1.1], "high": [1.2], "low": [1.0], "close": [1.1]}
    )

    with pytest.raises(ValueError, match="not timezone-aware"):
        _candles_from_frame(frame)


def test_validator_reports_total_years_and_shortfall(tmp_path, capsys) -> None:
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2026-06-30T08:00:00Z", "2026-06-30T09:00:00Z"]
            ),
            "open": [1.1, 1.1],
            "high": [1.2, 1.2],
            "low": [1.0, 1.0],
            "close": [1.1, 1.1],
        }
    )
    frame.to_parquet(tmp_path / "EURUSD_H1.parquet", engine="pyarrow")

    assert validate_cache(tmp_path, SESSIONS, design_years=0.1) == 1
    output = capsys.readouterr().out
    assert "TOTAL YEARS COVERED:" in output
    assert "coverage shortfall" in output


def test_year_coverage_shortfall_is_a_failure(capsys) -> None:
    candles = (
        _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
        _candle(datetime(2026, 6, 30, 9, tzinfo=UTC)),
    )

    assert _validate_one_cache(candles, "EURUSD", "H1", SESSIONS, (), 6)
    assert "coverage shortfall" in capsys.readouterr().out
