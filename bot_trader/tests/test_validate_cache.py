from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.history_gaps import (
    KnownHistoryGap,
    candle_is_in_session,
)
from tools.validate_cache import (
    CacheCandle,
    _candles_from_frame,
    _validate_one_cache,
    _weekly_boundary_checks,
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

    assert validate_cache(
        tmp_path,
        SESSIONS,
        design_years=0.1,
        coverage_start_utc=datetime(2026, 6, 30, 8, tzinfo=UTC),
    ) == 1
    output = capsys.readouterr().out
    assert "TOTAL YEARS COVERED:" in output
    assert "coverage shortfall" in output
    assert "COVERAGE BASIS: expected start through last closed candle" in output


def test_year_coverage_shortfall_is_a_failure(capsys) -> None:
    candles = (
        _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
        _candle(datetime(2026, 6, 30, 9, tzinfo=UTC)),
    )

    assert _validate_one_cache(candles, "EURUSD", "H1", SESSIONS, (), 6)
    assert "coverage shortfall" in capsys.readouterr().out


def _week_boundary_candles(
    week_start: datetime,
    timeframe: str,
    *,
    open_delta: timedelta = timedelta(0),
    last_delta: timedelta = timedelta(0),
) -> tuple[CacheCandle, CacheCandle]:
    is_summer = week_start.month in (4, 5, 6, 7, 8, 9, 10)
    open_hour = 21 if is_summer else 22
    h1_last_hour = 20 if is_summer else 21
    last_hour = h1_last_hour - (3 if timeframe == "H4" else 0)
    open_at = week_start.replace(hour=open_hour) + open_delta
    last_at = (week_start + timedelta(days=5)).replace(hour=last_hour) + last_delta
    return _candle(open_at), _candle(last_at)


@pytest.mark.parametrize(
    ("week_start", "timeframe"),
    [
        (datetime(2026, 1, 4, tzinfo=UTC), "H1"),
        (datetime(2026, 7, 5, tzinfo=UTC), "H1"),
    ],
)
def test_week_open_one_hour_late_without_approval_fails(
    week_start: datetime,
    timeframe: str,
) -> None:
    candles = _week_boundary_candles(
        week_start,
        timeframe,
        open_delta=timedelta(hours=1),
    )

    checks = _weekly_boundary_checks(
        candles,
        symbol="EURUSD",
        timeframe=timeframe,
        current_utc=week_start + pd.Timedelta(days=7),
    )

    assert [check.status for check in checks] == ["FAIL"]
    assert "open" in checks[0].detail


@pytest.mark.parametrize(
    ("week_start", "timeframe"),
    [
        (datetime(2026, 1, 4, tzinfo=UTC), "H1"),
        (datetime(2026, 7, 5, tzinfo=UTC), "H1"),
    ],
)
def test_week_closing_early_without_approval_fails(
    week_start: datetime,
    timeframe: str,
) -> None:
    candles = _week_boundary_candles(
        week_start,
        timeframe,
        last_delta=-timedelta(hours=1),
    )

    checks = _weekly_boundary_checks(
        candles,
        symbol="EURUSD",
        timeframe=timeframe,
        current_utc=week_start + pd.Timedelta(days=7),
    )

    assert [check.status for check in checks] == ["FAIL"]
    assert "close" in checks[0].detail


@pytest.mark.parametrize(
    ("week_start", "timeframe"),
    [
        (datetime(2026, 1, 4, tzinfo=UTC), "H1"),
        (datetime(2026, 7, 5, tzinfo=UTC), "H1"),
    ],
)
def test_week_open_deviation_inside_approved_gap_is_explained(
    week_start: datetime,
    timeframe: str,
) -> None:
    expected_open_hour = 22 if week_start.month == 1 else 21
    expected_open = week_start.replace(hour=expected_open_hour)
    candles = _week_boundary_candles(
        week_start,
        timeframe,
        open_delta=timedelta(hours=1),
    )
    approved_gap = KnownHistoryGap(
        "EURUSD",
        timeframe,
        expected_open,
        expected_open + timedelta(hours=1),
        1,
        "approved weekly opening gap",
        "Lefa",
        "2026-11-12",
    )

    checks = _weekly_boundary_checks(
        candles,
        symbol="EURUSD",
        timeframe=timeframe,
        known_gaps=(approved_gap,),
        current_utc=week_start + pd.Timedelta(days=7),
    )

    assert [check.status for check in checks] == ["EXPLAINED"]
    assert "approved known_gaps.csv row" in checks[0].detail


@pytest.mark.parametrize(
    ("week_start", "expected_last"),
    [
        (datetime(2026, 1, 4, tzinfo=UTC), datetime(2026, 1, 9, 18, tzinfo=UTC)),
        (datetime(2026, 7, 5, tzinfo=UTC), datetime(2026, 7, 10, 17, tzinfo=UTC)),
    ],
)
def test_h4_last_candle_expected_open_time_passes(
    week_start: datetime,
    expected_last: datetime,
) -> None:
    open_candle, last_candle = _week_boundary_candles(week_start, "H4")

    assert last_candle.time == expected_last
    checks = _weekly_boundary_checks(
        (open_candle, last_candle),
        timeframe="H4",
        current_utc=week_start + pd.Timedelta(days=7),
    )

    assert [check.status for check in checks] == ["PASS"]


@pytest.mark.parametrize(
    ("week_start", "timeframe"),
    [
        (datetime(2026, 1, 4, tzinfo=UTC), "H1"),
        (datetime(2026, 7, 5, tzinfo=UTC), "H1"),
    ],
)
def test_midweek_first_and_current_incomplete_weeks_are_skipped(
    week_start: datetime,
    timeframe: str,
) -> None:
    _open_candle, last_candle = _week_boundary_candles(week_start, timeframe)
    midweek = _candle(week_start + pd.Timedelta(days=2))
    partial_checks = _weekly_boundary_checks(
        (midweek, last_candle),
        timeframe=timeframe,
        coverage_start_utc=midweek.time,
        current_utc=week_start + pd.Timedelta(days=7),
    )
    current_week = (
        datetime(2026, 7, 5, tzinfo=UTC)
        if week_start.month == 7
        else datetime(2026, 11, 8, tzinfo=UTC)
    )
    current_open, current_last = _week_boundary_candles(current_week, timeframe)
    incomplete_checks = _weekly_boundary_checks(
        (current_open, current_last),
        timeframe=timeframe,
        current_utc=current_week + pd.Timedelta(days=2),
    )

    assert [check.status for check in partial_checks] == ["SKIPPED"]
    assert "partial first week" in partial_checks[0].detail
    assert [check.status for check in incomplete_checks] == ["SKIPPED"]
    assert "current incomplete week" in incomplete_checks[0].detail


def test_coverage_uses_expected_start_and_last_closed_bar(capsys) -> None:
    expected_start = datetime(2020, 11, 8, tzinfo=UTC)
    week_start = datetime(2026, 11, 8, tzinfo=UTC)
    candles = _week_boundary_candles(week_start, "H1")

    _validate_one_cache(
        candles,
        "EURUSD",
        "H1",
        SESSIONS,
        (),
        6,
        coverage_start_utc=expected_start,
        current_utc=datetime(2026, 11, 14, tzinfo=UTC),
    )

    output = capsys.readouterr().out
    assert "coverage 6." in output
    assert "coverage shortfall" not in output


def test_weekly_summary_is_reported_per_symbol_and_timeframe(capsys) -> None:
    week_start = datetime(2026, 1, 4, tzinfo=UTC)
    candles = _week_boundary_candles(week_start, "H4")
    _validate_one_cache(
        candles,
        "EURUSD",
        "H4",
        SESSIONS,
        (),
        0,
        current_utc=week_start + timedelta(days=7),
    )

    assert "WEEKLY SUMMARY EURUSD H4: PASS=1 EXPLAINED=0 SKIPPED=0 FAIL=0" in (
        capsys.readouterr().out
    )
