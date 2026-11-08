from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from random import Random
from types import SimpleNamespace

import pytest

from bot.app.data import Candle
from bot.app.random_control import build_random_entry_schedule, propose_random_entry
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_b import StrategyBParams
from bot.core.types import ReasonCode, Side
from tools import random_control


def _candles(count: int = 650) -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    values = []
    for index in range(count):
        close = 1.1 + (index % 9) * 0.00001
        values.append(
            Candle(
                start + timedelta(hours=index),
                close,
                close + 0.0002,
                close - 0.0002,
                close,
                spread=0.00005,
            )
        )
    return tuple(values)


def test_random_entry_proposal_uses_strategy_a_risk_and_target_rules() -> None:
    proposal = propose_random_entry(_candles(), "a", Side.LONG, StrategyAParams())

    assert proposal.proposal is not None
    assert proposal.proposal.side is Side.LONG
    assert proposal.proposal.reason == "RANDOM_CONTROL"
    assert proposal.proposal.take_profit - proposal.proposal.entry == pytest.approx(
        2 * (proposal.proposal.entry - proposal.proposal.stop_loss)
    )


def test_random_entry_proposal_uses_strategy_b_mean_reversion_target() -> None:
    candles = list(_candles())
    candles[-2] = Candle(
        candles[-2].time, 1.1002, 1.1004, 1.0998, 1.1002, spread=0.00005
    )
    result = propose_random_entry(tuple(candles), "b", Side.LONG, StrategyBParams())

    assert result.proposal is None or (
        result.proposal.side is Side.LONG
        and result.proposal.take_profit > result.proposal.entry
    )
    if result.proposal is None:
        assert result.reason in {
            ReasonCode.REWARD_TOO_SMALL,
            ReasonCode.STOP_TOO_WIDE,
        }


def test_random_schedule_is_reproducible_and_matches_requested_candidate_counts() -> None:
    histories = {
        ("EURUSD", "H1"): _candles(),
        ("EURUSD", "H4"): _candles(),
    }

    first = build_random_entry_schedule(histories, {"EURUSD": 2}, "a", Random(7))
    repeated = build_random_entry_schedule(histories, {"EURUSD": 2}, "a", Random(7))

    assert first == repeated
    assert len(first) == 2
    assert {key[1] for key in first} <= {"H1", "H4"}
    assert all(key[2] != histories[(key[0], key[1])][-1].time for key in first)
    assert set(first.values()) <= {Side.LONG, Side.SHORT}


def test_random_schedule_rejects_missing_candidate_history() -> None:
    with pytest.raises(ValueError, match="no random-entry candidates"):
        build_random_entry_schedule({}, {"EURUSD": 1}, "a", Random(1))


def test_random_control_runs_reproducibly_and_reports_expectancy(tmp_path, monkeypatch) -> None:
    trades = (
        SimpleNamespace(
            ticket="position-1",
            symbol="EURUSD",
            closed_at_utc="2024-01-02T00:00:00+00:00",
            actual_risk=100.0,
        ),
    )
    deals = (
        SimpleNamespace(
            position_ticket="position-1",
            is_entry=True,
            profit=0.0,
            commission=5.0,
        ),
        SimpleNamespace(
            position_ticket="position-1",
            is_entry=False,
            profit=60.0,
            commission=5.0,
        ),
    )
    run_calls = []

    def fake_run_replay(*positional_args, **kwargs):
        assert len(positional_args) == 3
        run_calls.append(kwargs)
        return SimpleNamespace(trades=trades, deals=deals)

    monkeypatch.setattr(random_control, "run_replay", fake_run_replay)
    history = _candles(650)
    report = random_control.run_random_entry_control(
        {("EURUSD", "H1"): history},
        {},
        None,
        strategies=("a",),
        now_utc=datetime(2026, 2, 1, tzinfo=UTC),
        starting_equity=3_000.0,
        no_news=True,
        report_root=tmp_path,
        code_version="test",
        settings_version="test",
        runs=2,
        seed=11,
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    strategy_result = payload["strategies"]["a"]
    assert strategy_result["trade_targets"] == {"EURUSD": 1}
    assert strategy_result["completed_runs"] == 2
    assert strategy_result["baseline_expectancy_r"] == pytest.approx(0.5)
    assert strategy_result["random_expectancy_95th_percentile_r"] == pytest.approx(0.5)
    assert strategy_result["status"] == "INCONCLUSIVE"
    assert strategy_result["acceptance_run_count_met"] is False
    assert len(run_calls) == 3
