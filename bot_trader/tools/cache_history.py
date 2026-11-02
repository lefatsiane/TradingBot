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
    last_closed_candles,
    validate_history_end,
    write_history_cache,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache closed MT5 H1/H4 history as Parquet.")
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument("--start-utc", type=_parse_utc)
    parser.add_argument("--end-utc", type=_parse_utc)
    parser.add_argument("--cache-dir")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument(
        "--timeframes",
        nargs="+",
        choices=("H1", "H4"),
        default=("H1", "H4"),
    )
    args = parser.parse_args()

    settings_path = Path(args.settings).resolve()
    settings, _ = load_settings(settings_path)
    now_utc = datetime.now(tz=UTC)
    end_utc = args.end_utc or now_utc
    start_utc = args.start_utc or _years_before(end_utc, 6)
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

    broker = MT5ReadOnlyBroker(
        history_start_utc=start_utc,
        history_end_utc=end_utc,
    )
    broker.connect()
    failures: list[str] = []
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
                    closed = last_closed_candles(
                        candles,
                        timeframe,
                        now_utc=end_utc,
                    )
                    validate_history_end(
                        closed,
                        timeframe,
                        end_utc=end_utc,
                    )
                    manifest = write_history_cache(
                        cache_dir,
                        symbol,
                        timeframe,
                        closed,
                        downloaded_at_utc=now_utc,
                        symbol_spec=spec,
                    )
                except (DataQualityError, RuntimeError, ValueError) as error:
                    failures.append(f"{symbol} {timeframe}: {error}")
                    print(f"{symbol} {timeframe}: history cache failed: {error}")
                    continue
                print(
                    f"{symbol} {timeframe}: cached {manifest.bar_count} closed bars "
                    f"from {manifest.start_utc.isoformat()} through "
                    f"{manifest.end_utc.isoformat()}; "
                    f"market-gap intervals={manifest.gap_count}"
                )
    finally:
        broker.shutdown()
    if failures:
        raise SystemExit(f"History cache incomplete ({len(failures)} failures).")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("Timestamp must include a UTC offset of +00:00")
    return parsed.astimezone(UTC)


def _years_before(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


if __name__ == "__main__":
    main()
