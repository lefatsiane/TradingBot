from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.adapters.mt5_broker import MT5ReadOnlyBroker
from bot.app.config import load_settings
from bot.app.data import (
    DataQualityError,
    Timeframe,
    validate_history_end,
    write_history_cache,
)
from bot.app.history_gaps import (
    HistoryGap,
    inspect_history_gaps,
    load_holiday_windows,
    load_known_gaps,
)
from bot.core.types import Candle, SymbolSpec

_DEFAULT_START_UTC = datetime(2020, 11, 8, tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cache closed MT5 H1/H4 history as Parquet."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument(
        "--start-utc",
        type=_parse_utc,
        help=(
            "UTC inclusive start (default: 2020-11-08T00:00:00+00:00; "
            "the Sunday before the first full trading week)."
        ),
    )
    parser.add_argument("--end-utc", type=_parse_utc)
    parser.add_argument("--cache-dir")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument(
        "--known-gaps",
        help="Owner-approved CSV (default: config/known_gaps.csv beside settings).",
    )
    parser.add_argument(
        "--report-gaps",
        action="store_true",
        help="Download and list every unexpected gap without writing any cache.",
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        choices=("H1", "H4"),
        default=("H1", "H4"),
    )
    args = parser.parse_args(argv)

    settings_path = Path(args.settings).resolve()
    settings, _ = load_settings(settings_path)
    now_utc = datetime.now(tz=UTC)
    end_utc = args.end_utc or now_utc
    start_utc = args.start_utc or _DEFAULT_START_UTC
    if start_utc >= end_utc:
        parser.error("--start-utc must be earlier than --end-utc")
    enabled_symbols = {
        symbol for symbol, config in settings.symbols.items() if config.enabled
    }
    symbols = enabled_symbols if args.symbols is None else set(args.symbols)
    unknown = symbols - enabled_symbols
    if unknown:
        parser.error(f"Symbols must be enabled in settings: {', '.join(sorted(unknown))}")
    cache_dir = (
        Path(args.cache_dir).resolve()
        if args.cache_dir
        else settings_path.parent / "data" / "history"
    )
    known_gaps_path = (
        Path(args.known_gaps).resolve()
        if args.known_gaps
        else settings_path.parent / "config" / "known_gaps.csv"
    )
    try:
        known_gaps = load_known_gaps(known_gaps_path)
        holiday_windows = load_holiday_windows(
            settings_path.parent / "config" / "broker_holidays.csv"
        )
    except (OSError, ValueError) as error:
        parser.error(f"Could not load gap approvals ({type(error).__name__}): {error}")

    broker = MT5ReadOnlyBroker(
        history_start_utc=start_utc,
        history_end_utc=end_utc,
    )
    broker.connect()
    failures: list[str] = []
    pending: list[
        tuple[
            str,
            Timeframe,
            tuple[Candle, ...],
            SymbolSpec,
            tuple[dict[str, object], ...],
        ]
    ] = []
    reported_gaps: list[HistoryGap] = []
    try:
        for symbol in sorted(symbols):
            try:
                spec = broker.symbol_info(symbol)
            except (RuntimeError, ValueError) as error:
                failures.append(f"{symbol}: {error}")
                print(f"{symbol}: history cache failed: {error}")
                continue
            for timeframe in args.timeframes:
                try:
                    candles = broker.candles(symbol, timeframe)
                    interval = timedelta(hours=1 if timeframe == "H1" else 4)
                    closed = tuple(
                        candle
                        for candle in candles
                        if candle.time + interval <= end_utc
                    )
                    if not closed:
                        raise DataQualityError(
                            "No fully closed candles in the requested UTC range"
                        )
                    gaps, _ = inspect_history_gaps(
                        closed,
                        symbol,
                        timeframe,
                        settings.broker_session_hours_server_time,
                        known_gaps,
                        holiday_windows=holiday_windows,
                    )
                except (DataQualityError, RuntimeError, ValueError) as error:
                    failures.append(f"{symbol} {timeframe}: {error}")
                    print(f"{symbol} {timeframe}: history cache failed: {error}")
                    continue
                unexpected = [gap for gap in gaps if not gap.accepted]
                reported_gaps.extend(gaps)
                if unexpected:
                    failures.extend(
                        f"{symbol} {timeframe}: unexpected gap "
                        f"{gap.start_utc.isoformat()} through {gap.end_utc.isoformat()} "
                        f"({gap.missing_candles} candles)"
                        for gap in unexpected
                    )
                accepted = tuple(gap.as_dict() for gap in gaps if gap.accepted)
                if args.report_gaps:
                    if not unexpected:
                        print(f"{symbol} {timeframe}: no unexpected gaps")
                    for gap in gaps:
                        if gap.accepted:
                            _print_gap(gap, prefix="APPROVED")
                    continue
                if unexpected:
                    continue
                try:
                    validate_history_end(
                        closed,
                        timeframe,
                        end_utc=end_utc,
                        accepted_gaps=accepted,
                    )
                except (DataQualityError, ValueError) as error:
                    failures.append(f"{symbol} {timeframe}: {error}")
                    print(f"{symbol} {timeframe}: history cache failed: {error}")
                    continue
                pending.append((symbol, timeframe, closed, spec, accepted))
        if args.report_gaps:
            _print_gaps_grouped(reported_gaps)
            print(
                "Gap report complete; no cache files were written."
            )
        elif failures:
            print("Cache not written because one or more history jobs failed.")
        else:
            for symbol, timeframe, closed, spec, accepted in pending:
                manifest = write_history_cache(
                    cache_dir,
                    symbol,
                    timeframe,
                    closed,
                    downloaded_at_utc=now_utc,
                    symbol_spec=spec,
                    accepted_gaps=accepted,
                    design_years=6,
                )
                print(
                    f"{symbol} {timeframe}: cached {manifest.bar_count} closed bars "
                    f"from {manifest.start_utc.isoformat()} through "
                    f"{manifest.end_utc.isoformat()}; "
                    f"years covered={manifest.years_covered:.3f}/"
                    f"{manifest.design_years}; accepted gaps="
                    f"{len(manifest.accepted_gaps)}"
                )
    finally:
        broker.shutdown()
    if failures:
        print(f"History cache incomplete ({len(failures)} failures).", file=sys.stderr)
        return 1
    return 0


def _print_gaps_grouped(gaps: list[HistoryGap]) -> None:
    categories = (
        "holiday window",
        "clock-change weekend",
        "known single gap",
        "unexpected",
    )
    unexpected_count = sum(not gap.accepted for gap in gaps)
    print(f"GAP SUMMARY: {len(gaps)} total; {unexpected_count} unexpected")
    for category in categories:
        selected = [gap for gap in gaps if gap.category == category]
        print(f"\n{category.upper()} ({len(selected)})")
        for gap in selected:
            prefix = "APPROVED" if gap.accepted else "UNEXPECTED"
            _print_gap(gap, prefix=prefix)


def _print_gap(gap: HistoryGap, *, prefix: str) -> None:
    weekdays = ", ".join(gap.weekdays)
    reason = f"; {gap.reason}" if gap.reason else ""
    approval = (
        f"; approved by {gap.approved_by} on {gap.approved_on}: "
        f"{gap.approval_reason}"
        if gap.accepted
        else ""
    )
    classification = (
        f"; draft classification: {gap.source_category}"
        if gap.source_category and not gap.accepted
        else ""
    )
    print(
        f"{prefix} GAP {gap.symbol} {gap.timeframe}: "
        f"{gap.start_utc.isoformat()} to {gap.end_utc.isoformat()} (exclusive); "
        f"{gap.missing_candles} missing candle(s); weekday(s): {weekdays}"
        f"{reason}{approval}{classification}"
    )


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("Timestamp must include a UTC offset of +00:00")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
