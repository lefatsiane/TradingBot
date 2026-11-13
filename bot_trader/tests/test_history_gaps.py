from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.data import DataQualityError, read_history_cache, write_history_cache
from bot.app.history_gaps import (
    KnownHolidayWindow,
    inspect_history_gaps,
    load_holiday_windows,
    load_known_gaps,
)
from bot.core.types import Candle, SymbolSpec

SESSIONS = BrokerSessionHoursServerTime()
SPEC = SymbolSpec(
    "EURUSD", "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
    contract_size=100_000,
)


def _candle(at: datetime) -> Candle:
    return Candle(at, 1.1, 1.101, 1.099, 1.1)


def test_finds_every_unexpected_gap_with_weekday_and_exclusive_end() -> None:
    candles = (
        _candle(datetime(2021, 12, 2, 7, tzinfo=UTC)),
        _candle(datetime(2021, 12, 2, 14, tzinfo=UTC)),
        _candle(datetime(2021, 12, 2, 16, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(candles, "EURUSD", "H1", SESSIONS)

    assert len(gaps) == 2
    assert (
        gaps[0].start_utc,
        gaps[0].end_utc,
        gaps[0].missing_candles,
        gaps[0].weekdays,
    ) == (
        datetime(2021, 12, 2, 8, tzinfo=UTC),
        datetime(2021, 12, 2, 14, tzinfo=UTC),
        6,
        ("Thursday",),
    )
    assert gaps[1].start_utc == datetime(2021, 12, 2, 15, tzinfo=UTC)


def test_known_gap_only_accepts_exact_approved_midweek_run_of_two(tmp_path: Path) -> None:
    approved_path = tmp_path / "known_gaps.csv"
    approved_path.write_text(
        "# owner approved\n"
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "EURUSD,H1,2026-06-30T09:00:00+00:00,2026-06-30T11:00:00+00:00,"
        "2,broker outage,owner,2026-11-11\n",
        encoding="utf-8",
    )
    approved = load_known_gaps(approved_path)
    candles = (
        _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
        _candle(datetime(2026, 6, 30, 11, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(candles, "EURUSD", "H1", SESSIONS, approved)

    assert len(gaps) == 1
    assert gaps[0].known
    assert gaps[0].accepted
    assert gaps[0].weekdays == ("Tuesday",)


def test_known_gap_over_two_candles_is_not_accepted(tmp_path: Path) -> None:
    approved_path = tmp_path / "known_gaps.csv"
    approved_path.write_text(
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "EURUSD,H1,2021-12-02T08:00:00+00:00,2021-12-02T14:00:00+00:00,"
        "6,broker outage,owner,2026-11-11\n",
        encoding="utf-8",
    )
    approved = load_known_gaps(approved_path)
    candles = (
        _candle(datetime(2021, 12, 2, 7, tzinfo=UTC)),
        _candle(datetime(2021, 12, 2, 14, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(candles, "EURUSD", "H1", SESSIONS, approved)

    assert gaps[0].known
    assert not gaps[0].accepted


def test_approved_single_gap_can_be_outside_midweek(tmp_path: Path) -> None:
    approved_path = tmp_path / "known_gaps.csv"
    approved_path.write_text(
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "EURUSD,H1,2026-07-03T10:00:00+00:00,2026-07-03T11:00:00+00:00,"
        "1,broker outage,owner,2026-11-11\n",
        encoding="utf-8",
    )
    candles = (
        _candle(datetime(2026, 7, 3, 9, tzinfo=UTC)),
        _candle(datetime(2026, 7, 3, 11, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(
        candles,
        "EURUSD",
        "H1",
        SESSIONS,
        load_known_gaps(approved_path),
    )

    assert gaps[0].known
    assert gaps[0].accepted


def test_h4_gap_count_uses_four_hour_slots() -> None:
    candles = (
        _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
        _candle(datetime(2026, 6, 30, 16, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(candles, "EURUSD", "H4", SESSIONS)

    assert len(gaps) == 1
    assert gaps[0].start_utc == datetime(2026, 6, 30, 12, tzinfo=UTC)
    assert gaps[0].end_utc == datetime(2026, 6, 30, 16, tzinfo=UTC)
    assert gaps[0].missing_candles == 1


@pytest.mark.parametrize(
    ("transition_date", "before_offset", "after_offset"),
    [
        ("2021-03-14", 2, 3),
        ("2021-11-07", 3, 2),
        ("2022-03-13", 2, 3),
        ("2022-11-06", 3, 2),
        ("2023-03-12", 2, 3),
        ("2023-11-05", 3, 2),
        ("2024-03-10", 2, 3),
        ("2024-11-03", 3, 2),
        ("2025-03-09", 2, 3),
        ("2025-11-02", 3, 2),
        ("2026-03-08", 2, 3),
        ("2026-11-01", 3, 2),
    ],
)
@pytest.mark.parametrize(("timeframe", "before_hour"), [("H1", 23), ("H4", 20)])
def test_dst_weekend_alignment_uses_server_wall_intervals(
    timeframe: str,
    transition_date: str,
    before_offset: int,
    after_offset: int,
    before_hour: int,
) -> None:
    transition_day = datetime.fromisoformat(transition_date).date()
    friday = datetime.combine(
        transition_day - timedelta(days=2),
        datetime.min.time().replace(hour=before_hour),
        tzinfo=UTC,
    ) - timedelta(hours=before_offset)
    monday = datetime.combine(
        transition_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    ) - timedelta(hours=after_offset)

    gaps, expected_closures = inspect_history_gaps(
        (_candle(friday), _candle(monday)),
        "EURUSD",
        timeframe,
        SESSIONS,
    )

    assert gaps == ()
    assert expected_closures == 1


def test_draft_single_gap_rows_are_not_accepted(tmp_path: Path) -> None:
    path = tmp_path / "known_gaps.csv"
    path.write_text(
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "NZDUSD,H1,2023-03-12T21:00:00+00:00,2023-03-12T22:00:00+00:00,"
        "1,reported single missing candle,,\n",
        encoding="utf-8",
    )
    known = load_known_gaps(path)
    gaps, _ = inspect_history_gaps(
        (
            _candle(datetime(2023, 3, 12, 20, tzinfo=UTC)),
            _candle(datetime(2023, 3, 12, 22, tzinfo=UTC)),
        ),
        "NZDUSD",
        "H1",
        SESSIONS,
        known,
    )

    assert len(known) == 1
    assert not known[0].approved
    assert len(gaps) == 1
    assert gaps[0].start_utc == datetime(2023, 3, 12, 21, tzinfo=UTC)
    assert all(not gap.accepted for gap in gaps)


def test_holiday_gap_requires_exactly_containing_approved_window(
    tmp_path: Path,
) -> None:
    path = tmp_path / "broker_holidays.csv"
    path.write_text(
        "start_utc,end_utc,reason,approved_by,approved_on\n"
        "2021-12-02T08:00:00+00:00,2021-12-02T14:00:00+00:00,"
        "broker holiday,owner,2026-11-11\n",
        encoding="utf-8",
    )
    windows = load_holiday_windows(path)
    candles = (
        _candle(datetime(2021, 12, 2, 7, tzinfo=UTC)),
        _candle(datetime(2021, 12, 2, 14, tzinfo=UTC)),
    )

    gaps, _ = inspect_history_gaps(
        candles,
        "EURUSD",
        "H1",
        SESSIONS,
        holiday_windows=windows,
    )

    assert len(gaps) == 1
    assert gaps[0].accepted
    assert gaps[0].category == "holiday window"
    assert gaps[0].missing_candles == 6

    too_narrow = (
        KnownHolidayWindow(
            datetime(2021, 12, 2, 9, tzinfo=UTC),
            datetime(2021, 12, 2, 14, tzinfo=UTC),
            "narrow approval",
            "owner",
            "2026-11-11",
        ),
    )
    outside_gap, _ = inspect_history_gaps(
        candles,
        "EURUSD",
        "H1",
        SESSIONS,
        holiday_windows=too_narrow,
    )
    assert not outside_gap[0].accepted
    assert outside_gap[0].category == "unexpected"


def test_draft_holiday_gap_remains_unexpected(tmp_path: Path) -> None:
    path = tmp_path / "broker_holidays.csv"
    path.write_text(
        "start_utc,end_utc,reason,approved_by,approved_on\n"
        "2021-12-02T08:00:00+00:00,2021-12-02T14:00:00+00:00,"
        "draft holiday,,\n",
        encoding="utf-8",
    )

    gaps, _ = inspect_history_gaps(
        (
            _candle(datetime(2021, 12, 2, 7, tzinfo=UTC)),
            _candle(datetime(2021, 12, 2, 14, tzinfo=UTC)),
        ),
        "EURUSD",
        "H1",
        SESSIONS,
        holiday_windows=load_holiday_windows(path),
    )

    assert not gaps[0].accepted
    assert gaps[0].category == "holiday window"
    assert gaps[0].source_category == "holiday window"


def test_checked_in_owner_approved_gap_and_holiday_rows_are_loaded() -> None:
    config_dir = Path(__file__).parents[1] / "config"
    known = load_known_gaps(config_dir / "known_gaps.csv")
    holidays = load_holiday_windows(config_dir / "broker_holidays.csv")

    assert len(known) == 13
    assert all(gap.approved for gap in known)
    assert len(holidays) == 14
    assert all(holiday.approved for holiday in holidays)


def test_partial_approval_metadata_is_rejected(tmp_path: Path) -> None:
    known_path = tmp_path / "known_gaps.csv"
    known_path.write_text(
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "EURUSD,H1,2026-03-31T21:00:00+00:00,2026-03-31T22:00:00+00:00,"
        "1,reported gap,owner,\n",
        encoding="utf-8",
    )
    holiday_path = tmp_path / "broker_holidays.csv"
    holiday_path.write_text(
        "start_utc,end_utc,reason,approved_by,approved_on\n"
        "2021-12-24T00:00:00+00:00,2021-12-26T00:00:00+00:00,"
        "Christmas,owner,\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="both be set or empty"):
        load_known_gaps(known_path)
    with pytest.raises(ValueError, match="both be set or empty"):
        load_holiday_windows(holiday_path)


def test_wrong_bounds_or_count_do_not_match_owner_approval(tmp_path: Path) -> None:
    approved_path = tmp_path / "known_gaps.csv"
    approved_path.write_text(
        "symbol,timeframe,start_utc,end_utc,missing_candles,reason,approved_by,approved_on\n"
        "EURUSD,H1,2026-06-30T10:00:00+00:00,2026-06-30T11:00:00+00:00,"
        "1,broker outage,owner,2026-11-11\n",
        encoding="utf-8",
    )
    gaps, _ = inspect_history_gaps(
        (
            _candle(datetime(2026, 6, 30, 8, tzinfo=UTC)),
            _candle(datetime(2026, 6, 30, 10, tzinfo=UTC)),
        ),
        "EURUSD",
        "H1",
        SESSIONS,
        load_known_gaps(approved_path),
    )

    assert len(gaps) == 1
    assert not gaps[0].known
    assert not gaps[0].accepted


def test_approved_gap_is_persisted_and_round_trips_in_manifest(tmp_path: Path) -> None:
    start = datetime(2026, 6, 30, 8, tzinfo=UTC)
    end = start + timedelta(hours=3)
    gap = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "start_utc": (start + timedelta(hours=1)).isoformat(),
        "end_utc": end.isoformat(),
        "missing_candles": 2,
        "weekdays": ["Tuesday"],
        "known": True,
        "accepted": True,
        "reason": None,
        "approval_reason": "broker outage",
        "approved_by": "owner",
        "approved_on": "2026-11-11",
    }

    manifest = write_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        (_candle(start), _candle(end)),
        downloaded_at_utc=end + timedelta(hours=1),
        symbol_spec=SPEC,
        accepted_gaps=(gap,),
    )
    with pytest.raises(DataQualityError, match="not approved"):
        read_history_cache(tmp_path, "EURUSD", "H1")
    _, loaded = read_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        owner_approved_gaps=(
            {
                "symbol": "EURUSD",
                "timeframe": "H1",
                "start_utc": (start + timedelta(hours=1)).isoformat(),
                "end_utc": end.isoformat(),
                "missing_candles": 2,
                "reason": "broker outage",
                "approved_by": "owner",
                "approved_on": "2026-11-11",
            },
        ),
    )

    assert manifest.accepted_gaps == (gap,)
    assert loaded.accepted_gaps == (gap,)
    assert loaded.design_years == 6
    assert loaded.years_covered == manifest.years_covered


def test_approved_holiday_gap_round_trips_and_requires_matching_approval(
    tmp_path: Path,
) -> None:
    holiday = {
        "start_utc": "2021-12-02T08:00:00+00:00",
        "end_utc": "2021-12-02T14:00:00+00:00",
        "reason": "broker holiday",
        "approved_by": "owner",
        "approved_on": "2026-11-11",
    }
    gap = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "start_utc": "2021-12-02T08:00:00+00:00",
        "end_utc": "2021-12-02T14:00:00+00:00",
        "missing_candles": 6,
        "weekdays": ["Thursday"],
        "known": True,
        "accepted": True,
        "reason": None,
        "approval_reason": holiday["reason"],
        "approved_by": holiday["approved_by"],
        "approved_on": holiday["approved_on"],
        "category": "holiday window",
        "source_category": "holiday window",
        "holiday_start_utc": holiday["start_utc"],
        "holiday_end_utc": holiday["end_utc"],
    }
    start = datetime(2021, 12, 2, 7, tzinfo=UTC)
    end = datetime(2021, 12, 2, 14, tzinfo=UTC)

    manifest = write_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        (_candle(start), _candle(end)),
        downloaded_at_utc=end + timedelta(hours=1),
        symbol_spec=SPEC,
        accepted_gaps=(gap,),
    )
    with pytest.raises(DataQualityError, match="holiday gap not approved"):
        read_history_cache(tmp_path, "EURUSD", "H1")
    _, loaded = read_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        owner_approved_holidays=(holiday,),
    )

    assert manifest.accepted_gaps == (gap,)
    assert loaded.accepted_gaps == (gap,)


def test_cache_refuses_gap_that_is_not_marked_accepted(tmp_path: Path) -> None:
    start = datetime(2026, 6, 30, 8, tzinfo=UTC)
    gap = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "start_utc": (start + timedelta(hours=1)).isoformat(),
        "end_utc": (start + timedelta(hours=3)).isoformat(),
        "missing_candles": 2,
        "known": False,
        "accepted": False,
    }

    with pytest.raises(DataQualityError, match="invalid accepted gap"):
        write_history_cache(
            tmp_path,
            "EURUSD",
            "H1",
            (_candle(start), _candle(start + timedelta(hours=3))),
            downloaded_at_utc=start + timedelta(hours=4),
            accepted_gaps=(gap,),
        )
