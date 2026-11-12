from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.adapters.broker import SymbolSpec
from bot.app.config import Settings, load_settings
from bot.app.data import Candle, DataQualityError, Timeframe, read_history_cache
from bot.app.history_gaps import load_holiday_windows, load_known_gaps
from bot.app.replay_runner import (
    claim_final_holdout_lock,
    run_replay,
)
from bot.app.versioning import get_version
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from tools.random_control import run_random_entry_control
from tools.stats import record_variant


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
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="Rerun selected strategies with each numeric parameter varied by about 20 percent.",
    )
    parser.add_argument(
        "--random-control",
        action="store_true",
        help="Run the 1,000-run random-entry control for the selected strategy or both.",
    )
    parser.add_argument(
        "--random-runs",
        type=int,
        default=1_000,
        help="Random-entry runs per strategy (default: 1000).",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=0,
        help="Seed for reproducible random-entry schedules (default: 0).",
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
    try:
        owner_approved_gaps = tuple(
            gap.as_dict()
            for gap in load_known_gaps(
                settings_path.parent / "config" / "known_gaps.csv"
            )
        )
        owner_approved_holidays = tuple(
            holiday.as_dict()
            for holiday in load_holiday_windows(
                settings_path.parent / "config" / "broker_holidays.csv"
            )
        )
    except (OSError, ValueError) as error:
        parser.error(f"Could not load history approvals ({type(error).__name__}): {error}")
    timeframes: tuple[Timeframe, ...] = (
        settings.strategy_a.timeframes
        if args.strategy == "a"
        else ("H1",)
        if args.strategy == "b"
        else tuple(dict.fromkeys((*settings.strategy_a.timeframes, "H1")))
    )
    histories = {}
    specs = {}
    accepted_gaps: list[dict[str, object]] = []
    try:
        for symbol, symbol_settings in settings.symbols.items():
            if not symbol_settings.enabled:
                continue
            for timeframe in timeframes:
                candles, manifest = read_history_cache(
                    data_dir,
                    symbol,
                    timeframe,
                    owner_approved_gaps=owner_approved_gaps,
                    owner_approved_holidays=owner_approved_holidays,
                )
                if manifest.symbol_spec is None:
                    parser.error(
                        f"{symbol} {timeframe} manifest has no symbol specification; "
                        "re-download with tools/cache_history.py."
                    )
                previous_spec = specs.setdefault(symbol, manifest.symbol_spec)
                if previous_spec != manifest.symbol_spec:
                    parser.error(f"{symbol} manifests disagree on symbol specification.")
                histories[(symbol, timeframe)] = candles
                accepted_gaps.extend(manifest.accepted_gaps)
    except DataQualityError as error:
        parser.error(str(error))

    now = datetime.now(tz=UTC)
    try:
        version = get_version(PROJECT_ROOT)
        code_version = version.commit + ("-dirty" if not version.clean else "")
        if args.random_control:
            if args.final or args.sensitivity:
                parser.error("--random-control cannot be combined with --final or --sensitivity")
            strategies = (
                ("a", "b") if args.strategy == "both" else (args.strategy,)
            )
            report_path = run_random_entry_control(
                histories,
                specs,
                settings,
                strategies=strategies,
                now_utc=now,
                starting_equity=args.starting_equity,
                no_news=args.no_news,
                report_root=reports_dir,
                code_version=code_version,
                settings_version=settings_version,
                cost_multiplier=args.cost_multiplier,
                runs=args.random_runs,
                seed=args.random_seed,
                accepted_gaps=tuple(accepted_gaps),
            )
            print(f"Saved random-entry control: {report_path}")
            return
        if args.sensitivity:
            if args.final:
                parser.error("--sensitivity cannot be combined with --final")
            strategies = (
                ("a", "b") if args.strategy == "both" else (args.strategy,)
            )
            sensitivity_path = _run_sensitivity(
                strategies,
                histories,
                specs,
                settings,
                now_utc=now,
                starting_equity=args.starting_equity,
                no_news=args.no_news,
                report_root=reports_dir,
                code_version=code_version,
                settings_version=settings_version,
                cost_multiplier=args.cost_multiplier,
                accepted_gaps=tuple(accepted_gaps),
            )
            print(f"Saved sensitivity results: {sensitivity_path}")
            return
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
            accepted_gaps=tuple(accepted_gaps),
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


def _run_sensitivity(
    strategies: tuple[Literal["a", "b"], ...],
    histories: Mapping[tuple[str, Timeframe], tuple[Candle, ...]],
    specs: Mapping[str, SymbolSpec],
    settings: Settings,
    *,
    now_utc: datetime,
    starting_equity: float,
    no_news: bool,
    report_root: Path,
    code_version: str,
    settings_version: str,
    cost_multiplier: float,
    accepted_gaps: tuple[dict[str, object], ...] = (),
) -> Path:
    rows: list[dict[str, object]] = []
    for strategy in strategies:
        if strategy == "a":
            base_params = settings.strategy_a.to_params()
            parameter_type = StrategyAParams
        else:
            base_params = settings.strategy_b.to_params()
            parameter_type = StrategyBParams
        for parameter in fields(parameter_type):
            original_value = getattr(base_params, parameter.name)
            if isinstance(original_value, bool) or not isinstance(
                original_value, (int, float)
            ):
                continue
            for direction, multiplier in (("minus20", 0.8), ("plus20", 1.2)):
                variant_value = (
                    max(1, round(original_value * multiplier))
                    if isinstance(original_value, int)
                    else original_value * multiplier
                )
                variant_id = (
                    f"sensitivity-{strategy}-{parameter.name}-{direction}-"
                    f"{uuid4().hex[:12]}"
                )
                description = (
                    f"Strategy {strategy.upper()} {parameter.name}: "
                    f"{original_value} -> {variant_value} ({direction})"
                )
                try:
                    candidate = replace(
                        base_params,
                        **{parameter.name: variant_value},
                    )
                except ValueError as error:
                    rows.append(
                        {
                            "strategy": strategy.upper(),
                            "parameter": parameter.name,
                            "baseline_value": original_value,
                            "variant_value": variant_value,
                            "status": "INVALID",
                            "report_dir": "",
                            "ending_equity": "",
                            "closed_trades": "",
                            "details": str(error),
                        }
                    )
                    continue
                if isinstance(candidate, StrategyAParams):
                    result = run_replay(
                        histories,
                        specs,
                        settings,
                        now_utc=now_utc,
                        starting_equity=starting_equity,
                        no_news=no_news,
                        strategy_mode="a",
                        report_root=report_root,
                        code_version=code_version,
                        settings_version=settings_version,
                        cost_multiplier=cost_multiplier,
                        strategy_a_params=candidate,
                        accepted_gaps=accepted_gaps,
                    )
                else:
                    result = run_replay(
                        histories,
                        specs,
                        settings,
                        now_utc=now_utc,
                        starting_equity=starting_equity,
                        no_news=no_news,
                        strategy_mode="b",
                        report_root=report_root,
                        code_version=code_version,
                        settings_version=settings_version,
                        cost_multiplier=cost_multiplier,
                        strategy_b_params=candidate,
                        accepted_gaps=accepted_gaps,
                    )
                record_variant(
                    report_root / "variants.json",
                    variant_id,
                    description,
                    recorded_at_utc=now_utc,
                )
                rows.append(
                    {
                        "strategy": strategy.upper(),
                        "parameter": parameter.name,
                        "baseline_value": original_value,
                        "variant_value": variant_value,
                        "status": "COMPLETED",
                        "report_dir": result.report_dir or "",
                        "ending_equity": result.ending_equity,
                        "closed_trades": sum(
                            deal.is_entry is False for deal in result.deals
                        ),
                        "details": (
                            f"{description}; parameter is subject to the "
                            "1,000-candle engine cap"
                            if parameter.name == "window_candles"
                            and variant_value > 1_000
                            else description
                        ),
                    }
                )

    report_root.mkdir(parents=True, exist_ok=True)
    destination = (
        report_root
        / f"sensitivity-{now_utc:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.csv"
    )
    with destination.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "strategy",
                "parameter",
                "baseline_value",
                "variant_value",
                "status",
                "report_dir",
                "ending_equity",
                "closed_trades",
                "details",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    return destination


if __name__ == "__main__":
    main()
