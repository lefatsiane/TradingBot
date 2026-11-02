from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.adapters.broker import Tick
from bot.app.data import (
    DataQualityError,
    fetch_history,
    last_closed_candles,
    read_history_cache,
    validate_candles,
    validate_history_end,
    write_history_cache,
)
from bot.core.types import Candle, SymbolSpec

START = datetime(2026, 1, 5, 10, tzinfo=UTC)
NOW = START + timedelta(hours=1)
DEFAULT_TICK = Tick(NOW, 1.1, 1.1001)
EURUSD = SymbolSpec(
    "EURUSD",
    "EUR",
    "USD",
    0.00001,
    0.0001,
    10,
    0.01,
    0.01,
    100,
    contract_size=100_000,
)


class HistoryBroker:
    def __init__(
        self,
        candles: tuple[Candle, ...],
        tick: Tick | None = DEFAULT_TICK,
    ) -> None:
        self.candle_data = candles
        self.current_tick = tick

    def tick(self, symbol: str) -> Tick | None:
        return self.current_tick

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]:
        return self.candle_data


def fixture_candles() -> tuple[Candle, ...]:
    return (
        Candle(START, 1.1, 1.101, 1.099, 1.1005, spread=0.0001, tick_volume=10),
        Candle(NOW, 1.1005, 1.102, 1.100, 1.101, spread=0.0002, tick_volume=12),
    )


def test_fetch_history_checks_tick_freshness_and_candle_quality() -> None:
    candles = fixture_candles()

    assert fetch_history(
        HistoryBroker(candles),
        "EURUSD",
        "H1",
        now_utc=NOW,
    ) == candles

    stale = Tick(NOW - timedelta(minutes=11), 1.1, 1.1001)
    with pytest.raises(DataQualityError, match="stale"):
        fetch_history(
            HistoryBroker(candles, stale),
            "EURUSD",
            "H1",
            now_utc=NOW,
        )


def test_last_closed_candles_excludes_current_incomplete_bar() -> None:
    candles = (
        Candle(START, 1.1, 1.101, 1.099, 1.1005),
        Candle(NOW, 1.1005, 1.102, 1.100, 1.101),
    )

    assert last_closed_candles(candles, "H1", now_utc=NOW + timedelta(minutes=59)) == (
        candles[0],
    )
    assert last_closed_candles(candles, "H1", now_utc=NOW + timedelta(hours=1)) == candles


def test_last_closed_candles_rejects_naive_time_and_empty_closed_history() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        last_closed_candles(fixture_candles(), "H1", now_utc=datetime(2026, 1, 5, 12))
    with pytest.raises(DataQualityError, match="no fully closed"):
        last_closed_candles(
            (fixture_candles()[0],),
            "H1",
            now_utc=START + timedelta(minutes=59),
        )


def test_history_end_must_be_fresh_and_not_future_dated() -> None:
    candles = fixture_candles()

    validate_history_end(candles, "H1", end_utc=NOW + timedelta(hours=1))
    with pytest.raises(DataQualityError, match="maximum allowed"):
        validate_history_end(candles, "H1", end_utc=NOW + timedelta(days=6))
    with pytest.raises(DataQualityError, match="closes after"):
        validate_history_end(candles, "H1", end_utc=NOW)


@pytest.mark.parametrize(
    "candles",
    [
        (fixture_candles()[1], fixture_candles()[0]),
        (fixture_candles()[0], fixture_candles()[0]),
        (
            fixture_candles()[0],
            Candle(NOW + timedelta(hours=2), 1.1, 1.101, 1.099, 1.1005),
        ),
    ],
)
def test_history_rejects_reordered_duplicate_or_gapped_candles(
    candles: tuple[Candle, ...],
) -> None:
    with pytest.raises(DataQualityError):
        validate_candles(candles, "H1")


def test_history_allows_expected_friday_to_sunday_gap() -> None:
    friday = Candle(
        datetime(2026, 1, 9, 21, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )
    sunday = Candle(
        datetime(2026, 1, 11, 22, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )

    validate_candles((friday, sunday), "H1")


def test_history_allows_bounded_holiday_weekend_gap() -> None:
    thursday = Candle(
        datetime(2026, 12, 24, 20, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )
    monday = Candle(
        datetime(2026, 12, 28, 0, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )

    validate_candles((thursday, monday), "H1")


@pytest.mark.parametrize(
    ("previous_time", "current_time"),
    [
        (datetime(2022, 12, 23, 23, tzinfo=UTC), datetime(2022, 12, 27, tzinfo=UTC)),
        (datetime(2024, 12, 24, 23, tzinfo=UTC), datetime(2024, 12, 26, tzinfo=UTC)),
        (datetime(2026, 3, 31, 23, tzinfo=UTC), datetime(2026, 4, 1, 1, tzinfo=UTC)),
    ],
)
def test_history_allows_known_holiday_and_rollover_closures(
    previous_time: datetime,
    current_time: datetime,
) -> None:
    previous = Candle(previous_time, 1.1, 1.101, 1.099, 1.1005)
    current = Candle(current_time, 1.1, 1.101, 1.099, 1.1005)

    validate_candles((previous, current), "H1")


def test_history_allows_one_missing_h1_bar_and_counts_it_in_manifest(tmp_path: Path) -> None:
    candles = (
        Candle(datetime(2026, 6, 29, 8, tzinfo=UTC), 1.1, 1.101, 1.099, 1.1005),
        Candle(datetime(2026, 6, 29, 10, tzinfo=UTC), 1.1, 1.101, 1.099, 1.1005),
    )
    manifest = write_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        candles,
        downloaded_at_utc=datetime(2026, 6, 29, 11, tzinfo=UTC),
        symbol_spec=EURUSD,
    )

    assert manifest.gap_count == 1
    _, loaded_manifest = read_history_cache(tmp_path, "EURUSD", "H1")
    assert loaded_manifest.gap_count == 1


def test_history_still_rejects_an_unexplained_weekday_gap() -> None:
    previous = Candle(
        datetime(2021, 12, 2, 10, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )
    current = Candle(
        datetime(2021, 12, 2, 16, tzinfo=UTC),
        1.1,
        1.101,
        1.099,
        1.1005,
    )

    with pytest.raises(DataQualityError, match="Unexpected H1 candle gap"):
        validate_candles((previous, current), "H1")


def test_history_cache_round_trip_preserves_candles_and_manifest(tmp_path: Path) -> None:
    candles = fixture_candles()

    manifest = write_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        candles,
        downloaded_at_utc=NOW,
        symbol_spec=EURUSD,
    )
    cached, loaded_manifest = read_history_cache(tmp_path, "EURUSD", "H1")

    assert cached == candles
    assert loaded_manifest == manifest
    assert manifest.bar_count == 2
    assert manifest.start_utc == START
    assert manifest.end_utc == NOW
    assert loaded_manifest.symbol_spec == EURUSD


def test_cache_rejects_manifest_mismatch_and_non_utc_download_time(
    tmp_path: Path,
) -> None:
    write_history_cache(
        tmp_path,
        "EURUSD",
        "H1",
        fixture_candles(),
        downloaded_at_utc=NOW,
    )
    manifest_path = tmp_path / "EURUSD_H1.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bar_count"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DataQualityError, match="does not match"):
        read_history_cache(tmp_path, "EURUSD", "H1")
    with pytest.raises(ValueError, match="UTC"):
        write_history_cache(
            tmp_path,
            "GBPUSD",
            "H1",
            fixture_candles(),
            downloaded_at_utc=NOW.astimezone(timezone(timedelta(hours=2))),
        )
