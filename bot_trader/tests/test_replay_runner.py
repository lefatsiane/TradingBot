from __future__ import annotations

import csv
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.adapters.broker import BrokerAccount
from bot.adapters.sim_broker import SimCosts
from bot.app.backtest import run_strategy_a_backtest
from bot.app.config import build_simulator_costs, load_settings
from bot.app.data import DataQualityError
from bot.app.replay_runner import (
    _simulator_costs,
    _tick_for_candle,
    claim_final_holdout_lock,
    run_replay,
)
from bot.core.indicators import ema
from bot.core.replay import ReplaySettings
from bot.core.risk import RiskLimits
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from bot.core.types import Candle, Side, SymbolSpec
from tools import replay as replay_tool

SPEC = SymbolSpec(
    "EURUSD",
    "EUR",
    "USD",
    0.00001,
    0.0001,
    10.0,
    0.01,
    0.01,
    100.0,
    contract_size=100_000.0,
)


def test_final_holdout_lock_is_exclusive_and_records_code_version(tmp_path) -> None:
    lock_path = tmp_path / "reports" / "final-holdout.lock"
    created = datetime(2026, 11, 3, 12, tzinfo=UTC)

    claim_final_holdout_lock(
        lock_path,
        code_version="abc123-dirty",
        created_at_utc=created,
    )

    assert json.loads(lock_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "code_version": "abc123-dirty",
        "created_at_utc": created.isoformat(),
    }
    with pytest.raises(ValueError, match="already locked"):
        claim_final_holdout_lock(
            lock_path,
            code_version="different-version",
            created_at_utc=created,
        )


def test_cost_multiplier_doubles_spread_and_slippage_not_commission() -> None:
    settings = _settings()
    candle = Candle(
        datetime(2026, 10, 26, 8, tzinfo=UTC),
        1.1,
        1.1002,
        1.0998,
        1.1,
        spread=0.00005,
    )
    base_tick = _tick_for_candle(candle)
    stressed_tick = _tick_for_candle(candle, spread_multiplier=2)
    base_cost = build_simulator_costs(settings, {"EURUSD"})["EURUSD"]
    stressed_cost = _simulator_costs(settings, ("EURUSD",), 2)["EURUSD"]

    assert stressed_tick.ask - stressed_tick.bid == pytest.approx(
        2 * (base_tick.ask - base_tick.bid)
    )
    assert stressed_cost.slippage_points == 2 * base_cost.slippage_points
    assert stressed_cost.commission_per_lot_side == base_cost.commission_per_lot_side


def _settings():
    settings, _ = load_settings(Path(__file__).parents[1] / "settings.toml")
    symbols = {
        name: configured.model_copy(update={"enabled": False})
        for name, configured in settings.symbols.items()
    }
    symbols["EURUSD"] = symbols["EURUSD"].model_copy(
        update={
            "enabled": True,
            "hours_utc": (
                "mon 00:00-21:55",
                "tue 00:00-21:55",
                "wed 00:00-21:55",
                "thu 00:00-21:55",
                "fri 00:00-21:55",
            ),
            "hours_utc_season": "US winter",
        }
    )
    return settings.model_copy(update={"symbols": symbols})


def _histories(start: datetime) -> dict[tuple[str, str], tuple[Candle, ...]]:
    h1 = tuple(
        Candle(
            start + timedelta(hours=index),
            1.1,
            1.1002,
            1.0998,
            1.1001,
            spread=0.0001,
        )
        for index in range(4)
    )
    h4 = tuple(
        Candle(
            start + timedelta(hours=index * 4),
            1.1,
            1.1002,
            1.0998,
            1.1001,
            spread=0.0001,
        )
        for index in range(1)
    )
    return {("EURUSD", "H1"): h1, ("EURUSD", "H4"): h4}


def test_engine_replay_uses_simulator_and_writes_auditable_report(tmp_path) -> None:
    settings = _settings()
    account = BrokerAccount(
        login=settings.account.expected_login,
        server=settings.account.expected_server,
        currency="USD",
        balance=3_000.0,
        equity=3_000.0,
        is_demo=True,
        trade_allowed=True,
        margin_mode="hedging",
    )
    start = datetime(2026, 10, 26, 8, tzinfo=UTC)
    result = run_replay(
        _histories(start),
        {"EURUSD": SPEC},
        settings,
        account=account,
        now_utc=start + timedelta(hours=4),
        final=True,
        no_news=True,
        strategy_mode="a",
        strategy_a_params=StrategyAParams(ema_fast=16),
        report_root=tmp_path,
        code_version="unit-test-commit",
        settings_version="unit-test-settings",
        cost_multiplier=2.0,
    )

    assert result.news_enabled is False
    assert result.decisions
    assert result.ending_equity == 3_000.0
    assert result.deals == ()
    assert result.report_dir is not None
    assert (result.report_dir / "trades.csv").is_file()
    assert (result.report_dir / "equity_curve.csv").is_file()
    assert (result.report_dir / "summary.json").is_file()
    summary = json.loads(
        (result.report_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["code_version"] == "unit-test-commit"
    assert summary["settings_version"] == "unit-test-settings"
    assert summary["cost_multiplier"] == 2.0
    assert summary["strategy_parameters"]["A"]["ema_fast"] == 16


def test_engine_replay_rejects_recent_history_without_final_flag() -> None:
    settings = _settings()
    account = BrokerAccount(
        login=settings.account.expected_login,
        server=settings.account.expected_server,
        currency="USD",
        balance=3_000.0,
        equity=3_000.0,
        is_demo=True,
        trade_allowed=True,
        margin_mode="hedging",
    )
    start = datetime(2026, 10, 26, 8, tzinfo=UTC)

    with pytest.raises(DataQualityError, match="most recent 12 months"):
        run_replay(
            _histories(start),
            {"EURUSD": SPEC},
            settings,
            account=account,
            now_utc=start + timedelta(hours=4),
            final=False,
            no_news=True,
        )


def test_sensitivity_writes_every_parameter_variant_and_records_completed_runs(
    tmp_path, monkeypatch
) -> None:
    calls = []

    def fake_run_replay(*positional_args, **kwargs):
        assert len(positional_args) == 3
        calls.append(kwargs)
        return SimpleNamespace(
            report_dir=tmp_path / f"report-{len(calls)}",
            ending_equity=3_000.0,
            deals=(),
        )

    monkeypatch.setattr(replay_tool, "run_replay", fake_run_replay)
    settings = _settings()
    start = datetime(2024, 10, 26, 8, tzinfo=UTC)
    result_path = replay_tool._run_sensitivity(
        ("a", "b"),
        _histories(start),
        {"EURUSD": SPEC},
        settings,
        now_utc=start + timedelta(hours=4),
        starting_equity=3_000.0,
        no_news=True,
        report_root=tmp_path,
        code_version="test",
        settings_version="test",
        cost_multiplier=1.0,
    )

    with result_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    expected_count = 2 * (
        len(fields(StrategyAParams)) + len(fields(StrategyBParams))
    )
    assert len(rows) == expected_count
    assert len(calls) + sum(row["status"] == "INVALID" for row in rows) == expected_count
    assert {kwargs["strategy_mode"] for kwargs in calls} == {"a", "b"}
    assert all(
        (kwargs.get("strategy_a_params") is None)
        != (kwargs.get("strategy_b_params") is None)
        for kwargs in calls
    )
    ledger = json.loads((tmp_path / "variants.json").read_text(encoding="utf-8"))
    assert len(ledger["variants"]) == len(calls)
    assert {row["status"] for row in rows} <= {"COMPLETED", "INVALID"}


def _parity_fixture(start: datetime, count: int) -> tuple[Candle, ...]:
    closes = [1.0 + index * 0.0001 for index in range(count)]
    ema20 = ema(closes, 20)
    candles: list[Candle] = []
    timestamp = start
    for close in closes:
        while timestamp.weekday() >= 5 or timestamp.hour >= 22:
            timestamp += timedelta(hours=1)
        candles.append(
            Candle(
                timestamp,
                close,
                close + 0.0003,
                close - 0.0003,
                close,
                spread=0.0001,
            )
        )
        timestamp += timedelta(hours=1)
    touched_index = count - 5
    candle = candles[touched_index]
    candles[touched_index] = Candle(
        candle.time,
        candle.open,
        candle.high,
        min(candle.low, ema20[touched_index]),
        candle.close,
        spread=candle.spread,
    )
    previous_index = count - 2
    candle = candles[previous_index]
    candles[previous_index] = Candle(
        candle.time,
        candle.open,
        closes[previous_index] + 0.00005,
        candle.low,
        candle.close,
        spread=candle.spread,
    )
    return tuple(candles)


def test_engine_replay_strategy_a_matches_legacy_fixture_at_95_percent(
    tmp_path,
) -> None:
    settings = _settings()
    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    h1 = _parity_fixture(start, 650)
    last = h1[-1]
    h1 = (
        *h1,
        Candle(
            last.time + timedelta(hours=1),
            last.close,
            last.close + 0.0003,
            last.close - 0.0003,
            last.close,
            spread=0.0001,
        ),
    )
    h4 = tuple(
        Candle(
            start + timedelta(hours=index * 4),
            1.1,
            1.1002,
            1.0998,
            1.1001,
            spread=0.0001,
        )
        for index in range(165)
    )
    now = h1[-1].time + timedelta(hours=1)
    replay = run_replay(
        {("EURUSD", "H1"): h1, ("EURUSD", "H4"): h4},
        {"EURUSD": SPEC},
        settings,
        now_utc=now,
        final=True,
        no_news=True,
        strategy_mode="a",
        report_root=tmp_path,
    )
    symbol_settings = settings.symbols["EURUSD"]
    legacy = run_strategy_a_backtest(
        h1,
        SPEC,
        settings.strategy_a.to_params(),
        timeframe="H1",
        now_utc=now,
        magic_number=settings.account.magic_number,
        settings=ReplaySettings(
            starting_equity=3_000.0,
            risk_per_trade_pct=settings.risk.risk_per_trade_pct,
            commission_per_lot_side=symbol_settings.commission_per_lot_side,
            slippage_price=symbol_settings.slippage_points * SPEC.point,
            typical_spread_price=0.0001,
            cost_gate_pct_of_risk=settings.risk.cost_gate_pct_of_risk,
        ),
        risk_limits=RiskLimits(
            daily_loss_pct=settings.risk.daily_loss_pct,
            monthly_loss_pct=settings.risk.monthly_loss_pct,
            max_net_currency_exposure_pct=settings.risk.max_net_currency_exposure_pct,
            max_open_trades=settings.risk.max_open_trades,
            max_margin_used_pct=settings.risk.max_margin_used_pct,
            cooldown_bars_after_stop=settings.risk.cooldown_bars_after_stop,
        ),
        costs=SimCosts(
            commission_per_lot_side=symbol_settings.commission_per_lot_side,
            slippage_points=symbol_settings.slippage_points,
        ),
        final=True,
    )
    legacy_entries = [
        (trade.side.value, trade.entry_time, round(trade.entry_price, 8))
        for trade in legacy.trades
    ]
    if legacy.open_trade is not None:
        legacy_entries.append(
            (
                legacy.open_trade.side.value,
                legacy.open_trade.entry_time,
                round(legacy.open_trade.entry_price, 8),
            )
        )
    replay_entries = [
        (deal.side.value, deal.time_utc, round(deal.price, 8))
        for deal in replay.deals
        if deal.is_entry is True
    ]
    legacy_trades = len(legacy_entries)
    replay_trades = len(replay_entries)
    matched_trades = sum(entry in replay_entries for entry in legacy_entries)
    parity_pct = (
        100.0
        if legacy_trades == replay_trades == 0
        else matched_trades / max(legacy_trades, replay_trades) * 100
    )

    assert legacy_trades > 0
    assert parity_pct == 100.0, (
        f"Strategy A replay parity was {parity_pct:.1f}% "
        f"({matched_trades}/{max(legacy_trades, replay_trades)} trades)"
    )
    assert replay.report_dir is not None
    with (replay.report_dir / "trades.csv").open(newline="", encoding="utf-8") as file:
        trade_rows = list(csv.DictReader(file))
    with (replay.report_dir / "portfolio_breakdown.csv").open(
        newline="", encoding="utf-8"
    ) as file:
        breakdown_rows = list(csv.DictReader(file))
    assert trade_rows
    assert all(row["strategy"] == "A" and row["timeframe"] == "H1" for row in trade_rows)
    assert any(row["grouping"] == "combined_A+B" for row in breakdown_rows)


def test_random_entry_schedule_runs_through_simulated_engine_and_records_target(
    tmp_path,
) -> None:
    settings = _settings()
    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    h1 = _parity_fixture(start, 650)
    last = h1[-1]
    h1 = (
        *h1,
        Candle(
            last.time + timedelta(hours=1),
            last.close,
            last.close + 0.0003,
            last.close - 0.0003,
            last.close,
            spread=0.0001,
        ),
    )
    h4 = tuple(
        Candle(
            start + timedelta(hours=index * 4),
            1.1,
            1.1002,
            1.0998,
            1.1001,
            spread=0.0001,
        )
        for index in range(165)
    )
    result = run_replay(
        {("EURUSD", "H1"): h1, ("EURUSD", "H4"): h4},
        {"EURUSD": SPEC},
        settings,
        now_utc=h1[-1].time + timedelta(hours=1),
        final=True,
        no_news=True,
        strategy_mode="a",
        random_entry_schedule={
            ("EURUSD", "H1", h1[-2].time): Side.LONG,
        },
        random_entry_targets={"EURUSD": 1},
        report_root=tmp_path,
    )

    assert len(result.trades) == 1
    assert result.trades[0].strategy == "A"
    assert result.random_entry_targets == {"EURUSD": 1}
    assert sum(deal.is_entry is True for deal in result.deals) == 1
