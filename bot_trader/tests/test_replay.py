from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.app.backtest import run_strategy_a_backtest, select_backtest_history
from bot.app.reports import backtest_metrics, write_backtest_report
from bot.core.replay import ReplaySettings
from bot.core.strategy_a import StrategyAParams
from bot.core.types import Candle, SymbolSpec

PARAMS = StrategyAParams(
    ema_fast=2,
    ema_mid=3,
    ema_slow=4,
    atr_period=2,
    slope_lookback=2,
    pullback_window=2,
    stop_lookback=2,
    stop_buffer_atr=0.1,
    min_stop_atr=0.1,
    max_stop_atr=5.0,
    min_candles=10,
    window_candles=10,
)
SPEC = SymbolSpec(
    "EURUSD",
    "EUR",
    "USD",
    0.01,
    0.01,
    5.0,
    0.01,
    0.01,
    100.0,
    contract_size=1.0,
)


def backtest_candles(extra_bars: int = 0) -> tuple[Candle, ...]:
    closes = [99, 99.5, 100, 100.5, 101, 102, 103, 102.5, 102.8, 104]
    start = datetime(2018, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            start + timedelta(hours=index),
            close - 0.05,
            close + 0.2,
            close - 0.2,
            close,
        )
        for index, close in enumerate(closes)
    ]
    candles.append(
        Candle(
            start + timedelta(hours=10),
            104.0,
            108.0,
            103.8,
            107.5,
        )
    )
    for index in range(extra_bars):
        stamp = start + timedelta(hours=11 + index)
        candles.append(Candle(stamp, 107.5, 107.6, 107.4, 107.5))
    return tuple(candles)


def run(candles: tuple[Candle, ...]):
    return run_strategy_a_backtest(
        candles,
        SPEC,
        PARAMS,
        timeframe="H1",
        now_utc=candles[-1].time + timedelta(hours=1),
        magic_number=7261026,
        settings=ReplaySettings(
            starting_equity=3_000.0,
            risk_per_trade_pct=0.5,
            commission_per_lot_side=0.0,
        ),
        final=True,
    )


def test_simulated_strategy_a_replay_is_deterministic_and_costs_are_accounted() -> None:
    first = run(backtest_candles())
    second = run(backtest_candles())

    assert first.trades == second.trades
    assert len(first.trades) == 1
    assert first.trades[0].exit_reason == "TARGET"
    assert first.trades[0].pnl > 0
    assert first.ending_equity == 3_000 + first.trades[0].pnl
    assert first.open_trade is None


def test_replay_does_not_change_earlier_trades_when_later_bars_are_added() -> None:
    original = run(backtest_candles())
    extended = run(backtest_candles(extra_bars=3))

    assert original.trades == extended.trades


def test_replay_excludes_recent_twelve_months_without_final_flag() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = tuple(
        Candle(start + timedelta(hours=index), 1.1, 1.101, 1.099, 1.1)
        for index in range(8_761)
    )
    now = datetime(2026, 1, 1, 1, tzinfo=UTC)

    training, excluded = select_backtest_history(
        candles,
        "H1",
        now_utc=now,
        final=False,
    )
    final_data, final_excluded = select_backtest_history(
        candles,
        "H1",
        now_utc=now,
        final=True,
    )

    assert len(training) == 1
    assert excluded == 8_760
    assert len(final_data) == 8_761
    assert final_excluded == 0


def test_report_saves_trades_curve_and_metrics(tmp_path: Path) -> None:
    result = run(backtest_candles())
    metrics = backtest_metrics(result)

    report_dir = write_backtest_report(
        tmp_path,
        result,
        created_at_utc=datetime(2026, 11, 1, tzinfo=UTC),
    )

    assert metrics.trade_count == 1
    assert metrics.win_rate_pct == 100
    assert (report_dir / "trades.csv").is_file()
    assert (report_dir / "equity_curve.csv").is_file()
    assert (report_dir / "summary.json").is_file()
