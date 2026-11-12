from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.app.config import load_settings
from bot.app.history_gaps import (
    HistoryGap,
    KnownHistoryGap,
    KnownHolidayWindow,
    candle_is_in_session,
    inspect_history_gaps,
    load_holiday_windows,
    load_known_gaps,
)
from bot.core.timeutil import expected_new_york_offset

_CACHE_NAME = re.compile(r"^(?P<symbol>[A-Za-z0-9]+)_(?P<timeframe>H1|H4)\.parquet$")
_YEAR_SECONDS = 365.2425 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class CacheCandle:
    time: datetime
    open: float
    high: float
    low: float
    close: float


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only validation of cached UTC history (no network or MT5)."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument("--cache-dir")
    parser.add_argument("--known-gaps")
    args = parser.parse_args(argv)
    settings_path = Path(args.settings).resolve()
    try:
        settings, _ = load_settings(settings_path)
        known_path = (
            Path(args.known_gaps).resolve()
            if args.known_gaps
            else settings_path.parent / "config" / "known_gaps.csv"
        )
        known_gaps = load_known_gaps(known_path)
        holiday_path = settings_path.parent / "config" / "broker_holidays.csv"
        holiday_windows = load_holiday_windows(holiday_path)
    except (OSError, ValueError) as error:
        parser.error(f"Could not load configuration or approved gaps: {error}")
    cache_dir = (
        Path(args.cache_dir).resolve()
        if args.cache_dir
        else settings_path.parent / "data" / "history"
    )
    return validate_cache(
        cache_dir,
        settings.broker_session_hours_server_time,
        known_gaps=known_gaps,
        holiday_windows=holiday_windows,
    )


def validate_cache(
    cache_dir: str | Path,
    sessions,
    *,
    known_gaps: tuple[KnownHistoryGap, ...] = (),
    holiday_windows: tuple[KnownHolidayWindow, ...] = (),
    design_years: float = 6.0,
) -> int:
    directory = Path(cache_dir)
    files = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
    cache_files = [
        (path, match.group("symbol"), match.group("timeframe"))
        for path in files
        if (match := _CACHE_NAME.fullmatch(path.name)) is not None
    ]
    if not cache_files:
        print(f"FAIL: no H1/H4 Parquet caches found in {directory}")
        return 1

    failed = False
    cache_years: list[float] = []
    for path, symbol, timeframe in cache_files:
        try:
            frame = pd.read_parquet(path, engine="pyarrow")
            candles = _candles_from_frame(frame)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"{symbol} {timeframe}: FAIL: could not read cache ({type(error).__name__})")
            failed = True
            continue
        if not candles:
            print(f"{symbol} {timeframe}: FAIL: no candles")
            failed = True
            continue
        coverage = _years_covered([candles[0].time, candles[-1].time])
        cache_years.append(coverage)
        manifest_path = path.with_suffix(".manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise TypeError("manifest is not a JSON object")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            manifest = None
            print(
                f"{symbol} {timeframe}: FAIL: manifest unavailable or invalid "
                f"({type(error).__name__})"
            )
            failed = True
        issues = _validate_one_cache(
            candles,
            symbol,
            timeframe,
            sessions,
            known_gaps,
            design_years,
            holiday_windows=holiday_windows,
            manifest=manifest,
        )
        failed |= issues

    years = min(cache_years) if cache_years else 0.0
    print(f"TOTAL YEARS COVERED: {years:.3f}/{design_years:.3f}")
    if years < design_years:
        print(f"FAIL: history coverage is short by {design_years - years:.3f} years")
        failed = True
    print("RESULT: FAIL" if failed else "RESULT: PASS")
    return 1 if failed else 0


def _validate_one_cache(
    candles: tuple[CacheCandle, ...],
    symbol: str,
    timeframe: str,
    sessions,
    known_gaps: tuple[KnownHistoryGap, ...],
    design_years: float,
    *,
    holiday_windows: tuple[KnownHolidayWindow, ...] = (),
    manifest: dict[str, object] | None = None,
) -> bool:
    failed = False
    first, last = candles[0].time, candles[-1].time
    years = _years_covered([first, last])
    print(
        f"{symbol} {timeframe}: first {first.isoformat()}, last {last.isoformat()}, "
        f"count {len(candles)}, years {years:.3f}/{design_years:.3f}"
    )
    counts = Counter(candle.time for candle in candles)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    out_of_order = [
        (previous.time, current.time)
        for previous, current in pairwise(candles)
        if current.time < previous.time
    ]
    failed |= _report_times("duplicate times", duplicates)
    failed |= _report_pairs("out-of-order times", out_of_order)

    outside = [
        candle
        for candle in candles
        if not candle_is_in_session(candle, timeframe, sessions)
    ]
    weekend_outside = [candle for candle in outside if candle.time.weekday() >= 5]
    failed |= _report_times(
        "Saturday/Sunday candles outside trading windows",
        [candle.time for candle in weekend_outside],
    )
    if outside:
        print(
            "  FAIL: candles outside session windows: "
            + ", ".join(candle.time.isoformat() for candle in outside)
        )
        failed = True
    else:
        print("  Session-window check: PASS")

    gaps, expected_count = inspect_history_gaps(
        candles,
        symbol,
        timeframe,
        sessions,
        known_gaps,
        holiday_windows=holiday_windows,
    )
    unexpected = [gap for gap in gaps if not gap.accepted]
    accepted = [gap for gap in gaps if gap.accepted]
    print(f"  Expected market-closure intervals: {expected_count}")
    print(f"  Known approved gaps accepted: {len(accepted)}")
    print(f"  Unexpected gaps: {len(unexpected)}")
    for gap in gaps:
        _print_gap(gap)
    failed |= bool(unexpected)
    if manifest is not None:
        manifest_gaps = manifest.get("accepted_gaps")
        expected_manifest_gaps = [gap.as_dict() for gap in accepted]
        if (
            manifest.get("symbol") != symbol
            or manifest.get("timeframe") != timeframe
            or manifest.get("bar_count") != len(candles)
            or manifest.get("start_utc") != first.isoformat()
            or manifest.get("end_utc") != last.isoformat()
            or manifest.get("design_years") != 6
            or not isinstance(manifest.get("years_covered"), (float, int))
            or abs(float(manifest["years_covered"]) - years) > 0.000001
            or manifest_gaps != expected_manifest_gaps
        ):
            print("  FAIL: cache manifest does not match data/approved gaps")
            failed = True

    bad_ohlc: list[datetime] = []
    bad_prices: list[datetime] = []
    for candle in candles:
        if (
            candle.high < candle.low
            or not candle.low <= candle.open <= candle.high
            or not candle.low <= candle.close <= candle.high
        ):
            bad_ohlc.append(candle.time)
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            bad_prices.append(candle.time)
    failed |= _report_times("invalid OHLC values", bad_ohlc)
    failed |= _report_times("zero or negative prices", bad_prices)

    bad_weeks = _weekly_boundary_failures(candles)
    if bad_weeks:
        print("  FAIL: weekly open/close:")
        for week in bad_weeks:
            print(f"    {week}")
        failed = True
    else:
        weeks = {
            candle.time.date()
            - timedelta(days=(candle.time.weekday() + 1) % 7)
            for candle in candles
        }
        print(f"  Weekly open/close check: PASS ({len(weeks)} weeks checked)")

    if years < design_years:
        print(f"  FAIL: coverage shortfall {design_years - years:.3f} years")
        failed = True
    return failed


def _weekly_boundary_failures(candles: tuple[CacheCandle, ...]) -> list[str]:
    week_groups: dict[date, list[CacheCandle]] = defaultdict(list)
    for candle in candles:
        week_start = candle.time.date() - timedelta(
            days=(candle.time.weekday() + 1) % 7
        )
        week_groups[week_start].append(candle)
    bad_weeks: list[str] = []
    for week_start, week_candles in sorted(week_groups.items()):
        week_candles.sort(key=lambda candle: candle.time)
        first_candle, last_candle = week_candles[0], week_candles[-1]
        first_hour = 21 if expected_new_york_offset(first_candle.time) == 3 else 22
        last_hour = 20 if expected_new_york_offset(last_candle.time) == 3 else 21
        if (
            first_candle.time.hour != first_hour
            or first_candle.time.minute != 0
            or last_candle.time.hour != last_hour
            or last_candle.time.minute != 0
        ):
            bad_weeks.append(
                f"{week_start}: open {first_candle.time.isoformat()} "
                f"(expected {first_hour:02d}:00), close {last_candle.time.isoformat()} "
                f"(expected {last_hour:02d}:00)"
            )
    return bad_weeks


def _candles_from_frame(frame: pd.DataFrame) -> tuple[CacheCandle, ...]:
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("cache is missing required candle columns")
    candles: list[CacheCandle] = []
    for row in frame.itertuples(index=False):
        timestamp = pd.Timestamp(row.time)
        if timestamp.tzinfo is None:
            raise ValueError("cached candle time is not timezone-aware")
        candles.append(
            CacheCandle(
                timestamp.to_pydatetime().astimezone(UTC),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
            )
        )
    return tuple(candles)


def _years_covered(times: list[datetime]) -> float:
    if len(times) < 2:
        return 0.0
    return (max(times) - min(times)).total_seconds() / _YEAR_SECONDS


def _report_times(label: str, values: list[datetime]) -> bool:
    if values:
        print(f"  FAIL: {label}: " + ", ".join(value.isoformat() for value in values))
        return True
    print(f"  {label}: none")
    return False


def _report_pairs(label: str, values: list[tuple[datetime, datetime]]) -> bool:
    if values:
        print(
            f"  FAIL: {label}: "
            + ", ".join(f"{first.isoformat()} -> {last.isoformat()}" for first, last in values)
        )
        return True
    print(f"  {label}: none")
    return False


def _print_gap(gap: HistoryGap) -> None:
    category = "KNOWN ACCEPTED" if gap.accepted else "UNEXPECTED"
    print(
        f"    {category} {gap.start_utc.isoformat()} to "
        f"{gap.end_utc.isoformat()} (exclusive); "
        f"{gap.missing_candles} candle(s); weekday(s): {', '.join(gap.weekdays)}"
        + (f"; {gap.reason}" if gap.reason else "")
        + (
            f"; approved by {gap.approved_by} on {gap.approved_on}: "
            f"{gap.approval_reason}"
            if gap.accepted
            else ""
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
