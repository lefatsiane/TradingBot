from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bot.app.backtest import BacktestResult


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    symbol: str
    timeframe: str
    net_profit: float
    win_rate_pct: float
    profit_factor: float | None
    max_drawdown: float
    max_drawdown_pct: float
    trade_count: int
    open_trade: bool


def backtest_metrics(result: BacktestResult) -> BacktestMetrics:
    winners = sum(trade.pnl for trade in result.trades if trade.pnl > 0)
    losers = sum(trade.pnl for trade in result.trades if trade.pnl < 0)
    win_count = sum(trade.pnl > 0 for trade in result.trades)
    peak = result.starting_equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for point in result.equity_curve:
        peak = max(peak, point.equity)
        drawdown = peak - point.equity
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak * 100)
    return BacktestMetrics(
        symbol=result.symbol,
        timeframe=result.timeframe,
        net_profit=result.ending_equity - result.starting_equity,
        win_rate_pct=(win_count / len(result.trades) * 100 if result.trades else 0.0),
        profit_factor=(winners / abs(losers) if losers else None),
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        trade_count=len(result.trades),
        open_trade=result.open_trade is not None,
    )


def write_backtest_report(
    report_root: str | Path,
    result: BacktestResult,
    *,
    created_at_utc: datetime | None = None,
) -> Path:
    created = created_at_utc or datetime.now(tz=UTC)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("created_at_utc must be timezone-aware")
    created = created.astimezone(UTC)
    run_name = f"{created:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    run_dir = Path(report_root) / result.symbol / result.timeframe / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    metrics = backtest_metrics(result)

    _write_csv(
        run_dir / "trades.csv",
        (
            "side",
            "lots",
            "signal_time",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "stop_loss",
            "take_profit",
            "actual_risk",
            "pnl",
            "result_r",
            "exit_reason",
        ),
        (
            {
                **asdict(trade),
                "side": trade.side.value,
                "signal_time": trade.signal_time.isoformat(),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
            }
            for trade in result.trades
        ),
    )
    _write_csv(
        run_dir / "equity_curve.csv",
        ("time_utc", "equity"),
        (
            {"time_utc": point.time_utc.isoformat(), "equity": point.equity}
            for point in result.equity_curve
        ),
    )
    summary = {
        "schema_version": 1,
        "created_at_utc": created.isoformat(),
        "starting_equity": result.starting_equity,
        "ending_equity": result.ending_equity,
        "excluded_recent_bars": result.excluded_recent_bars,
        "history_gap_count": result.history_gap_count,
        "skipped_cost": result.skipped_cost,
        "skipped_size": result.skipped_size,
        "skipped_risk": result.skipped_risk,
        "open_trade": asdict(result.open_trade) if result.open_trade else None,
        "metrics": asdict(metrics),
    }
    summary_path = run_dir / "summary.json"
    temp_path = summary_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temp_path, summary_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return run_dir


def _write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: Iterable[dict[str, object]],
) -> None:
    temp_path = path.with_suffix(".csv.tmp")
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
