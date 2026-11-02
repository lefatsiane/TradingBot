from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.adapters.sim_broker import SimCosts
from bot.app.backtest import run_strategy_a_backtest
from bot.app.config import load_settings
from bot.app.data import read_history_cache
from bot.app.reports import backtest_metrics, write_backtest_report
from bot.core.replay import ReplaySettings
from bot.core.risk import RiskLimits


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay Strategy A using cached closed history.")
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", choices=("H1", "H4"), default="H1")
    parser.add_argument("--cache-dir")
    parser.add_argument("--reports-dir")
    parser.add_argument("--starting-equity", type=float, default=3_000.0)
    parser.add_argument(
        "--final",
        action="store_true",
        help="Permit use of the most recent 12 months for the final evaluation run.",
    )
    args = parser.parse_args()

    settings_path = Path(args.settings).resolve()
    settings, _ = load_settings(settings_path)
    symbol_settings = settings.symbols.get(args.symbol)
    if symbol_settings is None or not symbol_settings.enabled:
        parser.error(f"{args.symbol} is not enabled in settings")
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
    candles, manifest = read_history_cache(data_dir, args.symbol, args.timeframe)
    if manifest.symbol_spec is None:
        parser.error(
            "History manifest has no symbol specification; re-download with "
            "tools/cache_history.py before replay."
        )

    risk = settings.risk
    result = run_strategy_a_backtest(
        candles,
        manifest.symbol_spec,
        settings.strategy_a.to_params(),
        timeframe=args.timeframe,
        now_utc=datetime.now(tz=UTC),
        magic_number=settings.account.magic_number,
        settings=ReplaySettings(
            starting_equity=args.starting_equity,
            risk_per_trade_pct=risk.risk_per_trade_pct,
            commission_per_lot_side=symbol_settings.commission_per_lot_side,
            slippage_price=symbol_settings.slippage_points * manifest.symbol_spec.point,
            cost_gate_pct_of_risk=risk.cost_gate_pct_of_risk,
        ),
        risk_limits=RiskLimits(
            daily_loss_pct=risk.daily_loss_pct,
            monthly_loss_pct=risk.monthly_loss_pct,
            max_net_currency_exposure_pct=risk.max_net_currency_exposure_pct,
            max_open_trades=risk.max_open_trades,
            max_margin_used_pct=risk.max_margin_used_pct,
            cooldown_bars_after_stop=risk.cooldown_bars_after_stop,
        ),
        costs=SimCosts(
            commission_per_lot_side=symbol_settings.commission_per_lot_side,
            slippage_points=symbol_settings.slippage_points,
        ),
        final=args.final,
        history_gap_count=manifest.gap_count,
    )
    run_dir = write_backtest_report(reports_dir, result)
    metrics = backtest_metrics(result)
    profit_factor = "n/a" if metrics.profit_factor is None else f"{metrics.profit_factor:.3f}"
    print(
        f"{metrics.symbol} {metrics.timeframe}: profit={metrics.net_profit:.2f}, "
        f"win_rate={metrics.win_rate_pct:.2f}%, profit_factor={profit_factor}, "
        f"max_drawdown={metrics.max_drawdown:.2f} ({metrics.max_drawdown_pct:.2f}%), "
        f"trades={metrics.trade_count}"
    )
    print(
        f"Excluded recent bars: {result.excluded_recent_bars}; "
        f"market-gap intervals: {result.history_gap_count}; "
        f"open trade at end: {'yes' if result.open_trade else 'no'}"
    )
    print(f"Saved replay report: {run_dir}")


if __name__ == "__main__":
    main()
