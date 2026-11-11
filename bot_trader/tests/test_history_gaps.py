from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.app.config import BrokerSessionHoursServerTime
from bot.app.data import DataQualityError, read_history_cache, write_history_cache
from bot.app.history_gaps import inspect_history_gaps, load_known_gaps
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


def test_known_gap_on_friday_is_not_accepted(tmp_path: Path) -> None:
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
    assert not gaps[0].accepted


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
