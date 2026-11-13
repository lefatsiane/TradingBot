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
_DEFAULT_START_UTC = datetime(2020, 11, 8, tzinfo=UTC)
_TIMEFRAME_INTERVAL = {"H1": timedelta(hours=1), "H4": timedelta(hours=4)}


@dataclass(frozen=True, slots=True)
class CacheCandle:
    time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class WeeklyBoundaryCheck:
    week_start: date
    status: str
    detail: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only validation of cached UTC history (no network or MT5)."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument("--cache-dir")
    parser.add_argument("--known-gaps")
    parser.add_argument(
        "--start-utc",
        type=_parse_utc,
        default=_DEFAULT_START_UTC,
        help=(
            "Expected UTC history start for coverage and partial-week checks "
            "(default: 2020-11-08T00:00:00+00:00)."
        ),
    )
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
        coverage_start_utc=args.start_utc,
    )


def validate_cache(
    cache_dir: str | Path,
    sessions,
    *,
    known_gaps: tuple[KnownHistoryGap, ...] = (),
    holiday_windows: tuple[KnownHolidayWindow, ...] = (),
    design_years: float = 6.0,
    coverage_start_utc: datetime = _DEFAULT_START_UTC,
    current_utc: datetime | None = None,
) -> int:
    now_utc = (current_utc or datetime.now(UTC)).astimezone(UTC)
    coverage_start = coverage_start_utc.astimezone(UTC)
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
        interval = _TIMEFRAME_INTERVAL[timeframe]
        coverage_end = candles[-1].time + interval
        coverage = _years_between(coverage_start, coverage_end)
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
            coverage_start_utc=coverage_start,
            current_utc=now_utc,
            manifest=manifest,
        )
        failed |= issues

    years = min(cache_years) if cache_years else 0.0
    print(
        "COVERAGE BASIS: expected start through last closed candle "
        "(last candle open + timeframe interval)."
    )
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
    coverage_start_utc: datetime | None = None,
    current_utc: datetime | None = None,
    manifest: dict[str, object] | None = None,
) -> bool:
    failed = False
    first, last = candles[0].time, candles[-1].time
    interval = _TIMEFRAME_INTERVAL[timeframe]
    coverage_start = (coverage_start_utc or first).astimezone(UTC)
    coverage_end = last + interval
    years = _years_between(coverage_start, coverage_end)
    data_span_years = _years_covered([first, last])
    print(
        f"{symbol} {timeframe}: first {first.isoformat()}, last {last.isoformat()}, "
        f"count {len(candles)}, coverage {years:.3f}/{design_years:.3f} years "
        f"({coverage_start.isoformat()} to last close {coverage_end.isoformat()})"
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
            or abs(float(manifest["years_covered"]) - data_span_years) > 0.000001
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

    weekly_checks = _weekly_boundary_checks(
        candles,
        symbol=symbol,
        timeframe=timeframe,
        known_gaps=known_gaps,
        holiday_windows=holiday_windows,
        coverage_start_utc=coverage_start_utc,
        current_utc=current_utc,
    )
    weekly_counts = Counter(check.status for check in weekly_checks)
    print(
        f"  WEEKLY SUMMARY {symbol} {timeframe}: "
        f"PASS={weekly_counts['PASS']} EXPLAINED={weekly_counts['EXPLAINED']} "
        f"SKIPPED={weekly_counts['SKIPPED']} FAIL={weekly_counts['FAIL']}"
    )
    for check in weekly_checks:
        if check.status != "PASS":
            print(f"    {check.status} {check.week_start}: {check.detail}")
    failed |= any(check.status == "FAIL" for check in weekly_checks)

    if years < design_years:
        print(f"  FAIL: coverage shortfall {design_years - years:.3f} years")
        failed = True
    return failed


def _weekly_boundary_failures(candles: tuple[CacheCandle, ...]) -> list[str]:
    return [
        check.detail
        for check in _weekly_boundary_checks(candles)
        if check.status == "FAIL"
    ]


def _weekly_boundary_checks(
    candles: tuple[CacheCandle, ...],
    *,
    symbol: str = "",
    timeframe: str = "H1",
    known_gaps: tuple[KnownHistoryGap, ...] = (),
    holiday_windows: tuple[KnownHolidayWindow, ...] = (),
    coverage_start_utc: datetime | None = None,
    current_utc: datetime | None = None,
) -> list[WeeklyBoundaryCheck]:
    week_groups: dict[date, list[CacheCandle]] = defaultdict(list)
    for candle in candles:
        week_start = candle.time.date() - timedelta(
            days=(candle.time.weekday() + 1) % 7
        )
        week_groups[week_start].append(candle)
    checks: list[WeeklyBoundaryCheck] = []
    now = (current_utc or datetime.now(UTC)).astimezone(UTC)
    first_week = min(week_groups) if week_groups else None
    start = coverage_start_utc.astimezone(UTC) if coverage_start_utc else None
    for week_start, week_candles in sorted(week_groups.items()):
        week_candles.sort(key=lambda candle: candle.time)
        first_candle, last_candle = week_candles[0], week_candles[-1]
        open_hour = _expected_week_open_hour(week_start)
        expected_open = datetime.combine(
            week_start, datetime.min.time(), tzinfo=UTC
        ) + timedelta(hours=open_hour)
        h1_last_hour = _expected_h1_last_hour(week_start)
        expected_last = datetime.combine(
            week_start + timedelta(days=5),
            datetime.min.time(),
            tzinfo=UTC,
        ) + timedelta(hours=h1_last_hour - (3 if timeframe == "H4" else 0))

        if (
            week_start == first_week
            and start is not None
            and start > expected_open
        ):
            checks.append(
                WeeklyBoundaryCheck(
                    week_start,
                    "SKIPPED",
                    f"partial first week: requested start {start.isoformat()} "
                    f"is after expected open {expected_open.isoformat()}",
                )
            )
            continue
        interval = _TIMEFRAME_INTERVAL[timeframe]
        if week_start == _week_start(now.date()) and expected_last + interval > now:
            checks.append(
                WeeklyBoundaryCheck(
                    week_start,
                    "SKIPPED",
                    f"current incomplete week: expected final candle closes "
                    f"{(expected_last + interval).isoformat()}",
                )
            )
            continue

        deviations: list[str] = []
        explanations: list[str] = []
        if first_candle.time != expected_open:
            approved = _approved_boundary_row(
                expected_open,
                symbol,
                timeframe,
                known_gaps,
                holiday_windows,
            )
            if approved:
                explanations.append(
                    f"expected open {expected_open.isoformat()} is covered by {approved}"
                )
            else:
                deviations.append(
                    f"open {first_candle.time.isoformat()} "
                    f"(expected {expected_open.isoformat()})"
                )
        if last_candle.time != expected_last:
            approved = _approved_boundary_row(
                expected_last,
                symbol,
                timeframe,
                known_gaps,
                holiday_windows,
            )
            if approved:
                explanations.append(
                    f"expected last {expected_last.isoformat()} is covered by {approved}"
                )
            else:
                deviations.append(
                    f"close {last_candle.time.isoformat()} "
                    f"(expected {expected_last.isoformat()})"
                )
        if deviations:
            checks.append(
                WeeklyBoundaryCheck(
                    week_start,
                    "FAIL",
                    f"{week_start}: " + "; ".join(deviations),
                )
            )
        elif explanations:
            checks.append(
                WeeklyBoundaryCheck(week_start, "EXPLAINED", "; ".join(explanations))
            )
        else:
            checks.append(WeeklyBoundaryCheck(week_start, "PASS"))
    return checks


def _expected_week_open_hour(week_start: date) -> int:
    candidate = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=21
    )
    return 21 if expected_new_york_offset(candidate) == 3 else 22


def _expected_h1_last_hour(week_start: date) -> int:
    friday = week_start + timedelta(days=5)
    candidate = datetime.combine(friday, datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=20
    )
    return 20 if expected_new_york_offset(candidate) == 3 else 21


def _approved_boundary_row(
    expected: datetime,
    symbol: str,
    timeframe: str,
    known_gaps: tuple[KnownHistoryGap, ...],
    holiday_windows: tuple[KnownHolidayWindow, ...],
) -> str | None:
    for holiday in holiday_windows:
        if (
            holiday.approved
            and holiday.start_utc <= expected < holiday.end_utc
        ):
            return f"approved broker_holidays.csv row {holiday.reason!r}"
    for gap in known_gaps:
        if (
            gap.approved
            and gap.symbol == symbol
            and gap.timeframe == timeframe
            and gap.start_utc <= expected < gap.end_utc
        ):
            return f"approved known_gaps.csv row {gap.reason!r}"
    return None


def _week_start(day: date) -> date:
    return day - timedelta(days=(day.weekday() + 1) % 7)


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


def _years_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / _YEAR_SECONDS)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("Timestamp must include a UTC offset of +00:00")
    return parsed.astimezone(UTC)


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
