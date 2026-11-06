from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.app.config import load_settings
from bot.app.data import DataQualityError, Timeframe, read_history_cache
from bot.app.replay_runner import claim_final_holdout_lock, run_replay
from bot.app.versioning import get_version


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay enabled symbols through the production engine and simulator."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument("--cache-dir")
    parser.add_argument("--reports-dir")
    parser.add_argument("--starting-equity", type=float, default=3_000.0)
    parser.add_argument(
        "--strategy",
        choices=("a", "b", "both"),
        default="both",
        help="Replay Strategy A, Strategy B, or the approved combined path.",
    )
    parser.add_argument(
        "--no-news",
        action="store_true",
        help="Disable news gating for history without matching calendar events; recorded in report.",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="Permit use of the most recent 12 months for the final evaluation run.",
    )
    parser.add_argument(
        "--cost-multiplier",
        type=float,
        default=1.0,
        help="Scale replay spreads and slippage; use 2.0 for doubled-cost stress.",
    )
    args = parser.parse_args()

    settings_path = Path(args.settings).resolve()
    settings, settings_version = load_settings(settings_path)
    data_dir = (
        Path(args.cache_dir).resolve()
        if args.cache_dir
        else settings_path.parent / "data" / "history"
    )
    reports_dir = (
        Path(args.reports_dir).resolve()
        if args.reports_dir
        else settings_path.parent / "data" / "reports"
    )
    timeframes: tuple[Timeframe, ...] = (
        settings.strategy_a.timeframes
        if args.strategy == "a"
        else ("H1",)
        if args.strategy == "b"
        else tuple(dict.fromkeys((*settings.strategy_a.timeframes, "H1")))
    )
    histories = {}
    specs = {}
    try:
        for symbol, symbol_settings in settings.symbols.items():
            if not symbol_settings.enabled:
                continue
            for timeframe in timeframes:
                candles, manifest = read_history_cache(data_dir, symbol, timeframe)
                if manifest.symbol_spec is None:
                    parser.error(
                        f"{symbol} {timeframe} manifest has no symbol specification; "
                        "re-download with tools/cache_history.py."
                    )
                previous_spec = specs.setdefault(symbol, manifest.symbol_spec)
                if previous_spec != manifest.symbol_spec:
                    parser.error(f"{symbol} manifests disagree on symbol specification.")
                histories[(symbol, timeframe)] = candles
    except DataQualityError as error:
        parser.error(str(error))

    now = datetime.now(tz=UTC)
    try:
        version = get_version(PROJECT_ROOT)
        code_version = version.commit + ("-dirty" if not version.clean else "")
        if args.final:
            claim_final_holdout_lock(
                reports_dir / "final-holdout.lock",
                code_version=code_version,
                created_at_utc=now,
            )
        result = run_replay(
            histories,
            specs,
            settings,
            now_utc=now,
            starting_equity=args.starting_equity,
            final=args.final,
            no_news=args.no_news,
            strategy_mode=args.strategy,
            report_root=reports_dir,
            code_version=code_version,
            settings_version=settings_version,
            cost_multiplier=args.cost_multiplier,
        )
    except (DataQualityError, ValueError, RuntimeError) as error:
        parser.error(str(error))

    closed_trades = sum(
        deal.is_entry is False
        for deal in result.deals
    )
    print(
        f"strategy={result.strategy_mode}, starting_equity={result.starting_equity:.2f}, "
        f"ending_equity={result.ending_equity:.2f}, closed_trades={closed_trades}, "
        f"decisions={len(result.decisions)}, news={'on' if result.news_enabled else 'off'}, "
        f"excluded_recent_bars={result.excluded_recent_bars}"
    )
    print(f"Saved replay report: {result.report_dir}")


if __name__ == "__main__":
    main()
