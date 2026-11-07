from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.stats import (
    BOOTSTRAP_RESAMPLES,
    analyze_report,
    build_scorecard,
    calculate_statistics,
    record_variant,
    write_scorecard,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_stats_cli_help_runs_from_project_root() -> None:
    result = subprocess.run(
        [sys.executable, "tools/stats.py", "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Analyze a replay report" in result.stdout


def test_statistics_use_candle_closes_and_calendar_week_bootstrap() -> None:
    points = [
        (datetime(2026, 1, 5, 12, tzinfo=UTC), 1_000),
        (datetime(2026, 1, 5, 14, tzinfo=UTC), 990),
        (datetime(2026, 1, 6, 12, tzinfo=UTC), 980),
        (datetime(2026, 2, 2, 12, tzinfo=UTC), 970),
        (datetime(2026, 2, 3, 12, tzinfo=UTC), 950),
    ]

    metrics = calculate_statistics(points, 1_000, resamples=100)

    assert metrics["candle_close_count"] == 5
    assert metrics["week_count"] == 2
    assert metrics["bootstrap_resamples"] == 100
    assert metrics["max_drawdown"] == 50
    assert metrics["max_drawdown_pct"] == 5
    assert metrics["worst_day_pct"] == pytest.approx((950 / 970 - 1) * 100)
    assert metrics["worst_month_pct"] == pytest.approx((950 / 980 - 1) * 100)
    assert metrics["weekly_compounded_return_ci_95_pct"]["lower"] <= metrics[
        "weekly_compounded_return_ci_95_pct"
    ]["upper"]
    assert BOOTSTRAP_RESAMPLES == 10_000


def test_scorecard_marks_missing_acceptance_evidence_inconclusive() -> None:
    scorecard = build_scorecard(
        {
            "worst_day_pct": -1.0,
            "worst_month_pct": -5.0,
        }
    )

    statuses = {item["measure"]: item["status"] for item in scorecard}
    assert statuses == {
        "weekly_compounded_return_pct": "INCONCLUSIVE",
        "worst_day_pct": "PASS",
        "worst_month_pct": "FAIL",
        "cost_drift_pct": "INCONCLUSIVE",
        "replay_parity_pct": "INCONCLUSIVE",
        "unplanned_restarts": "INCONCLUSIVE",
    }
    assert all("interval" in item and "threshold" in item for item in scorecard)


def test_write_scorecard_includes_date_range_and_versions(tmp_path) -> None:
    report_dir = tmp_path / "replay"
    report_dir.mkdir()
    (report_dir / "summary.json").write_text(
        json.dumps(
            {
                "starting_equity": 1_000,
                "code_version": "abc123",
                "settings_version": "def456",
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "equity_curve.csv").write_text(
        "time_utc,equity\n2026-01-05T12:00:00+00:00,1000\n"
        "2026-01-05T13:00:00+00:00,1005\n",
        encoding="utf-8",
    )

    destination = write_scorecard(report_dir)
    scorecard = json.loads(destination.read_text(encoding="utf-8"))

    assert scorecard["code_version"] == "abc123"
    assert scorecard["settings_version"] == "def456"
    assert scorecard["date_range_utc"] == {
        "start": "2026-01-05T12:00:00+00:00",
        "end": "2026-01-05T13:00:00+00:00",
    }


def test_cost_stress_comparison_uses_only_matching_reports(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    stress = tmp_path / "stress"
    baseline.mkdir()
    stress.mkdir()
    shared = {
        "strategy": "both",
        "news_enabled": True,
        "final_run": False,
        "starting_equity": 1_000,
        "excluded_recent_bars": 10,
        "code_version": "abc123",
        "settings_version": "def456",
    }
    (baseline / "summary.json").write_text(
        json.dumps({**shared, "cost_multiplier": 1.0, "net_profit": 100}),
        encoding="utf-8",
    )
    (stress / "summary.json").write_text(
        json.dumps({**shared, "cost_multiplier": 2.0, "net_profit": 70}),
        encoding="utf-8",
    )
    curve = (
        "time_utc,equity\n"
        "2026-01-05T12:00:00+00:00,1000\n"
        "2026-01-05T13:00:00+00:00,1100\n"
    )
    (baseline / "equity_curve.csv").write_text(curve, encoding="utf-8")
    (stress / "equity_curve.csv").write_text(curve, encoding="utf-8")

    scorecard = json.loads(
        write_scorecard(
            baseline,
            cost_stress_report=stress,
        ).read_text(encoding="utf-8")
    )
    cost_measure = next(
        item for item in scorecard["scorecard"] if item["measure"] == "cost_drift_pct"
    )

    assert cost_measure["value"] == pytest.approx(30)
    assert cost_measure["status"] == "FAIL"


def test_cost_stress_rejects_mismatched_reports(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    stress = tmp_path / "stress"
    baseline.mkdir()
    stress.mkdir()
    (baseline / "summary.json").write_text(
        json.dumps(
            {"cost_multiplier": 1, "starting_equity": 1_000, "strategy": "a"}
        ),
        encoding="utf-8",
    )
    (stress / "summary.json").write_text(
        json.dumps(
            {"cost_multiplier": 2, "starting_equity": 1_000, "strategy": "b"}
        ),
        encoding="utf-8",
    )
    curve = "time_utc,equity\n2026-01-05T12:00:00+00:00,1000\n"
    (baseline / "equity_curve.csv").write_text(curve, encoding="utf-8")
    (stress / "equity_curve.csv").write_text(curve, encoding="utf-8")

    with pytest.raises(ValueError, match="differ on strategy"):
        analyze_report(baseline, cost_stress_report=stress)


def test_variant_counter_persists_unique_trials_and_rejects_duplicate_ids(
    tmp_path,
) -> None:
    ledger = tmp_path / "reports" / "variants.json"
    recorded_at = datetime(2026, 11, 6, 15, tzinfo=UTC)

    assert record_variant(
        ledger,
        "strategy-a-ema-fast-24",
        "Strategy A ema_fast=24",
        recorded_at_utc=recorded_at,
    ) == 1
    assert record_variant(
        ledger,
        "strategy-b-rsi-oversold-36",
        "Strategy B rsi_oversold=36",
        recorded_at_utc=recorded_at,
    ) == 2
    with pytest.raises(ValueError, match="already recorded"):
        record_variant(
            ledger,
            "strategy-a-ema-fast-24",
            "Repeat",
            recorded_at_utc=recorded_at,
        )

    stored = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(stored["variants"]) == 2


def test_scorecard_reports_news_blocked_signals_overlapping_trades_and_variants(
    tmp_path,
) -> None:
    report_dir = tmp_path / "reports" / "replay"
    report_dir.mkdir(parents=True)
    report_root = report_dir.parent
    (report_root / "variants.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "variants": [{"variant_id": "a1"}, {"variant_id": "b1"}],
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "summary.json").write_text(
        json.dumps(
            {
                "starting_equity": 1_000,
                "code_version": "abc123",
                "settings_version": "def456",
                "news_enabled": True,
                "news_calendar_valid": True,
                "news_events": [
                    {
                        "time_utc": "2026-01-05T13:00:00+00:00",
                        "currency": "USD",
                        "impact": "high",
                        "title": "CPI",
                    }
                ],
                "news_block_before_min": 30,
                "news_block_after_min": 30,
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "equity_curve.csv").write_text(
        "time_utc,equity\n2026-01-05T12:00:00+00:00,1000\n"
        "2026-01-05T14:00:00+00:00,1005\n",
        encoding="utf-8",
    )
    (report_dir / "decisions.csv").write_text(
        "time_utc,symbol,reason,strategy,side,entry,stop_loss,take_profit\n"
        "2026-01-05T12:00:00+00:00,EURUSD,NEWS_WINDOW,A,,,,\n"
        "2026-01-05T13:00:00+00:00,EURUSD,ENTRY_ALLOWED,A,long,1.1,1.09,1.12\n",
        encoding="utf-8",
    )
    (report_dir / "trades.csv").write_text(
        "ticket,symbol,strategy,timeframe,side,lots,entry_time_utc,exit_time_utc,"
        "entry_price,exit_price,profit,commission,open\n"
        "1,EURUSD,A,H1,long,0.1,2026-01-05T12:45:00+00:00,"
        "2026-01-05T13:15:00+00:00,1.1,1.101,10,2,False\n"
        "2,GBPUSD,A,H1,long,0.1,2026-01-05T11:00:00+00:00,"
        "2026-01-05T12:00:00+00:00,1.2,1.199,-5,1,False\n",
        encoding="utf-8",
    )

    scorecard = json.loads(write_scorecard(report_dir).read_text(encoding="utf-8"))

    assert scorecard["variant_count"] == 2
    assert scorecard["news_metrics"] == {
        "status": "AVAILABLE",
        "news_gate_blocked_signals": 1,
        "event_count": 1,
        "overlapping_trade_count": 1,
        "overlapping_closed_trade_count": 1,
        "overlapping_open_trade_count": 0,
        "overlapping_net_profit": pytest.approx(8.0),
        "overlapping_winning_trades": 1,
        "overlapping_losing_trades": 0,
    }


def test_scorecard_marks_news_overlap_and_variant_count_unavailable(tmp_path) -> None:
    report_dir = tmp_path / "replay"
    report_dir.mkdir()
    (report_dir / "summary.json").write_text(
        json.dumps({"starting_equity": 1_000}),
        encoding="utf-8",
    )
    (report_dir / "equity_curve.csv").write_text(
        "time_utc,equity\n2026-01-05T12:00:00+00:00,1000\n",
        encoding="utf-8",
    )

    scorecard = analyze_report(report_dir)

    assert scorecard["variant_count"] is None
    assert scorecard["news_metrics"]["status"] == "UNAVAILABLE"
    assert scorecard["news_metrics"]["news_gate_blocked_signals"] is None


def test_scorecard_keeps_news_overlap_unavailable_for_invalid_calendar(tmp_path) -> None:
    report_dir = tmp_path / "replay"
    report_dir.mkdir()
    (report_dir / "summary.json").write_text(
        json.dumps(
            {
                "starting_equity": 1_000,
                "news_enabled": True,
                "news_calendar_valid": False,
                "news_events": [
                    {
                        "time_utc": "2026-01-05T13:00:00+00:00",
                        "currency": "USD",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "equity_curve.csv").write_text(
        "time_utc,equity\n2026-01-05T12:00:00+00:00,1000\n",
        encoding="utf-8",
    )
    (report_dir / "decisions.csv").write_text(
        "time_utc,symbol,reason\n"
        "2026-01-05T12:00:00+00:00,EURUSD,NEWS_WINDOW\n",
        encoding="utf-8",
    )

    scorecard = analyze_report(report_dir)

    assert scorecard["news_metrics"]["status"] == "UNAVAILABLE"
    assert scorecard["news_metrics"]["news_gate_blocked_signals"] == 1
    assert scorecard["news_metrics"]["event_count"] is None
