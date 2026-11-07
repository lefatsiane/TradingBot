from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.app.news import affected_currencies_for_symbol

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0


def calculate_statistics(
    points: list[tuple[datetime, float]],
    starting_equity: float,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    if not math.isfinite(starting_equity) or starting_equity <= 0:
        raise ValueError("starting_equity must be positive and finite")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not points:
        raise ValueError("equity curve must contain at least one point")
    previous_time: datetime | None = None
    for at, equity in points:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("equity timestamps must be timezone-aware")
        if previous_time is not None and at < previous_time:
            raise ValueError("equity timestamps must be ordered")
        if not math.isfinite(equity) or equity < 0:
            raise ValueError("equity values must be finite and non-negative")
        previous_time = at

    daily = _period_lows(
        points,
        lambda value: value.date(),
        starting_equity,
    )
    monthly = _period_lows(
        points,
        lambda value: (value.year, value.month),
        starting_equity,
    )
    peak = starting_equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for _, equity in points:
        if not math.isfinite(equity) or equity < 0:
            raise ValueError("equity values must be finite and non-negative")
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown / peak * 100)

    weekly_returns = _period_returns(points, lambda value: value.isocalendar()[:2], starting_equity)
    weekly_compounded_return = math.prod(1 + value for value in weekly_returns) - 1
    confidence_interval = _bootstrap_compounded_return(
        weekly_returns,
        resamples=resamples,
        seed=seed,
    )
    return {
        "candle_close_count": len(points),
        "week_count": len(weekly_returns),
        "weekly_compounded_return_pct": weekly_compounded_return * 100,
        "bootstrap_resamples": resamples if weekly_returns else 0,
        "bootstrap_seed": seed,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "worst_day_pct": min(daily.values(), default=0.0),
        "worst_month_pct": min(monthly.values(), default=0.0),
        "weekly_compounded_return_ci_95_pct": confidence_interval,
    }


def build_scorecard(
    metrics: dict[str, Any],
    *,
    cost_drift_pct: float | None = None,
) -> list[dict[str, Any]]:
    rows = [
        {
            "measure": "weekly_compounded_return_pct",
            "value": metrics.get("weekly_compounded_return_pct"),
            "interval": metrics.get("weekly_compounded_return_ci_95_pct"),
            "threshold": "not specified in the design record",
            "status": "INCONCLUSIVE",
        },
        _score(
            "worst_day_pct",
            metrics.get("worst_day_pct"),
            threshold=">= -1.5%",
            passed=lambda value: value >= -1.5,
        ),
        _score(
            "worst_month_pct",
            metrics.get("worst_month_pct"),
            threshold=">= -4.0%",
            passed=lambda value: value >= -4.0,
        ),
        _score(
            "cost_drift_pct",
            cost_drift_pct,
            threshold="absolute value <= 20%",
            passed=lambda value: abs(value) <= 20,
        ),
        _score(
            "replay_parity_pct",
            None,
            threshold=">= 95%",
            passed=lambda value: value >= 95,
        ),
        _score(
            "unplanned_restarts",
            None,
            threshold="<= 3",
            passed=lambda value: value <= 3,
        ),
    ]
    return rows


def analyze_report(
    report_dir: str | Path,
    *,
    cost_stress_report: str | Path | None = None,
) -> dict[str, Any]:
    directory = Path(report_dir)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    starting_equity = _finite_number(summary, "starting_equity")
    points = _read_equity_curve(directory / "equity_curve.csv")
    metrics = calculate_statistics(points, starting_equity)
    news_metrics = _news_metrics(
        directory,
        summary,
        report_end_utc=points[-1][0],
    )
    cost_drift_pct = None
    if cost_stress_report is not None:
        stress_dir = Path(cost_stress_report)
        stress_summary = json.loads(
            (stress_dir / "summary.json").read_text(encoding="utf-8")
        )
        _validate_stress_pair(summary, stress_summary)
        baseline_profit = _finite_number(summary, "net_profit")
        stress_profit = _finite_number(stress_summary, "net_profit")
        if baseline_profit > 0:
            cost_drift_pct = abs(stress_profit - baseline_profit) / baseline_profit * 100
    return {
        "schema_version": 1,
        "date_range_utc": {
            "start": points[0][0].isoformat(),
            "end": points[-1][0].isoformat(),
        },
        "code_version": summary.get("code_version", "unknown"),
        "settings_version": summary.get("settings_version", "unknown"),
        "variant_count": _variant_count(directory.parent / "variants.json"),
        "metrics": metrics,
        "news_metrics": news_metrics,
        "scorecard": build_scorecard(metrics, cost_drift_pct=cost_drift_pct),
    }


def write_scorecard(
    report_dir: str | Path,
    *,
    cost_stress_report: str | Path | None = None,
) -> Path:
    directory = Path(report_dir)
    result = analyze_report(directory, cost_stress_report=cost_stress_report)
    destination = directory / "scorecard.json"
    temporary = destination.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def record_variant(
    ledger_path: str | Path,
    variant_id: str,
    description: str,
    *,
    recorded_at_utc: datetime,
) -> int:
    if not variant_id.strip() or not description.strip():
        raise ValueError("variant_id and description must not be empty")
    recorded = recorded_at_utc
    if recorded.tzinfo is None or recorded.utcoffset() is None:
        raise ValueError("recorded_at_utc must be timezone-aware")
    recorded = recorded.astimezone(UTC)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("Variant ledger has an unsupported schema")
        variants = payload.get("variants")
        if not isinstance(variants, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("variant_id"), str)
            and isinstance(item.get("description"), str)
            and isinstance(item.get("recorded_at_utc"), str)
            for item in variants
        ):
            raise ValueError("Variant ledger has an invalid variants list")
    else:
        variants = []
    if any(item["variant_id"] == variant_id for item in variants):
        raise ValueError(f"Variant {variant_id!r} is already recorded")
    updated = {
        "schema_version": 1,
        "variants": [
            *variants,
            {
                "variant_id": variant_id,
                "description": description,
                "recorded_at_utc": recorded.isoformat(),
            },
        ],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(updated, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(updated["variants"])


def _validate_stress_pair(
    baseline: dict[str, Any],
    stress: dict[str, Any],
) -> None:
    if _finite_number(baseline, "cost_multiplier") != 1.0:
        raise ValueError("baseline report must use cost_multiplier=1")
    if _finite_number(stress, "cost_multiplier") != 2.0:
        raise ValueError("cost-stress report must use cost_multiplier=2")
    for key in (
        "strategy",
        "news_enabled",
        "final_run",
        "starting_equity",
        "excluded_recent_bars",
        "code_version",
        "settings_version",
    ):
        if baseline.get(key) != stress.get(key):
            raise ValueError(f"baseline and cost-stress reports differ on {key}")


def _read_equity_curve(path: Path) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if not {"time_utc", "equity"}.issubset(reader.fieldnames or ()):
            raise ValueError("equity_curve.csv must contain time_utc and equity columns")
        for row in reader:
            at = datetime.fromisoformat(row["time_utc"])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError("equity timestamps must be timezone-aware")
            equity = float(row["equity"])
            if not math.isfinite(equity) or equity < 0:
                raise ValueError("equity values must be finite and non-negative")
            at = at.astimezone(UTC)
            if points and at < points[-1][0]:
                raise ValueError("equity timestamps must be ordered")
            if points and at == points[-1][0]:
                points[-1] = (at, equity)
            else:
                points.append((at, equity))
    if not points:
        raise ValueError("equity_curve.csv contains no data")
    return points


def _variant_count(path: Path) -> int | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    variants = payload.get("variants") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(variants, list)
        or not all(
            isinstance(item, dict) and isinstance(item.get("variant_id"), str)
            for item in variants
        )
    ):
        raise ValueError(f"Variant ledger has an invalid schema: {path}")
    return len(variants)


def _news_metrics(
    report_dir: Path,
    summary: dict[str, Any],
    *,
    report_end_utc: datetime,
) -> dict[str, Any]:
    decisions_path = report_dir / "decisions.csv"
    if decisions_path.exists():
        with decisions_path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            if not {"time_utc", "reason"}.issubset(reader.fieldnames or ()):
                raise ValueError("decisions.csv must contain time_utc and reason columns")
            blocked_signals = sum(row.get("reason") == "NEWS_WINDOW" for row in reader)
    else:
        blocked_signals = None

    events = summary.get("news_events")
    news_enabled = summary.get("news_enabled")
    calendar_valid = summary.get("news_calendar_valid")
    if (
        news_enabled is not True
        or calendar_valid is not True
        or not isinstance(events, list)
    ):
        return {
            "status": "UNAVAILABLE",
            "news_gate_blocked_signals": blocked_signals,
            "event_count": None,
            "overlapping_trade_count": None,
            "overlapping_closed_trade_count": None,
            "overlapping_open_trade_count": None,
            "overlapping_net_profit": None,
            "overlapping_winning_trades": None,
            "overlapping_losing_trades": None,
        }

    before_min = _summary_int(summary, "news_block_before_min")
    after_min = _summary_int(summary, "news_block_after_min")
    normalized_events = []
    for event in events:
        if not isinstance(event, dict):
            raise TypeError("summary news_events entries must be objects")
        try:
            event_time = datetime.fromisoformat(event["time_utc"])
            currency = event["currency"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("summary contains an invalid news event") from error
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("summary news event timestamps must be timezone-aware")
        if not isinstance(currency, str):
            raise TypeError("summary news event currency must be a string")
        normalized_events.append((event_time, currency.upper()))

    trades_path = report_dir / "trades.csv"
    if not trades_path.exists():
        return {
            "status": "UNAVAILABLE",
            "news_gate_blocked_signals": blocked_signals,
            "event_count": len(events),
            "overlapping_trade_count": None,
            "overlapping_closed_trade_count": None,
            "overlapping_open_trade_count": None,
            "overlapping_net_profit": None,
            "overlapping_winning_trades": None,
            "overlapping_losing_trades": None,
        }

    overlapping: list[tuple[bool, float]] = []
    with trades_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        required = {
            "ticket",
            "symbol",
            "entry_time_utc",
            "exit_time_utc",
            "profit",
            "commission",
            "open",
        }
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError("trades.csv is missing news-analysis columns")
        for row in reader:
            if not row.get("entry_time_utc"):
                continue
            entry_time = _parse_aware_timestamp(row["entry_time_utc"])
            is_open = row["open"].strip().lower() == "true"
            exit_time = (
                report_end_utc
                if is_open
                else _parse_aware_timestamp(row["exit_time_utc"])
            )
            currencies = set(affected_currencies_for_symbol(row["symbol"]))
            has_overlap = any(
                currency in currencies
                and event_time - timedelta(minutes=before_min) <= exit_time
                and event_time + timedelta(minutes=after_min) >= entry_time
                for event_time, currency in normalized_events
            )
            if has_overlap:
                net_profit = float(row["profit"]) - float(row["commission"])
                if not math.isfinite(net_profit):
                    raise ValueError("trade P&L values must be finite")
                overlapping.append((is_open, net_profit))

    closed = [profit for is_open, profit in overlapping if not is_open]
    return {
        "status": "AVAILABLE",
        "news_gate_blocked_signals": blocked_signals,
        "event_count": len(events),
        "overlapping_trade_count": len(overlapping),
        "overlapping_closed_trade_count": len(closed),
        "overlapping_open_trade_count": len(overlapping) - len(closed),
        "overlapping_net_profit": sum(profit for _, profit in overlapping),
        "overlapping_winning_trades": sum(profit > 0 for profit in closed),
        "overlapping_losing_trades": sum(profit < 0 for profit in closed),
    }


def _summary_int(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"summary {key} must be a non-negative integer")
    return value


def _parse_aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Trade timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _period_lows(
    points: list[tuple[datetime, float]],
    key_for: Any,
    starting_equity: float,
) -> dict[Any, float]:
    groups: dict[Any, list[float]] = defaultdict(list)
    for at, equity in points:
        groups[key_for(at)].append(equity)
    results: dict[Any, float] = {}
    prior_equity = starting_equity
    for key in sorted(groups):
        values = groups[key]
        if prior_equity > 0:
            results[key] = (min(values) / prior_equity - 1) * 100
        prior_equity = values[-1]
    return results


def _period_returns(
    points: list[tuple[datetime, float]],
    key_for: Any,
    starting_equity: float,
) -> list[float]:
    groups: dict[Any, list[float]] = defaultdict(list)
    for at, equity in points:
        groups[key_for(at)].append(equity)
    returns: list[float] = []
    prior_equity = starting_equity
    for key in sorted(groups):
        end_equity = groups[key][-1]
        if prior_equity > 0:
            returns.append(end_equity / prior_equity - 1)
        prior_equity = end_equity
    return returns


def _bootstrap_compounded_return(
    weekly_returns: list[float],
    *,
    resamples: int,
    seed: int,
) -> dict[str, float] | None:
    if not weekly_returns:
        return None
    generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        compounded = 1.0
        for _ in weekly_returns:
            compounded *= 1 + weekly_returns[generator.randrange(len(weekly_returns))]
        estimates.append((compounded - 1) * 100)
    estimates.sort()
    return {
        "lower": _percentile(estimates, 0.025),
        "upper": _percentile(estimates, 0.975),
    }


def _percentile(values: list[float], quantile: float) -> float:
    index = (len(values) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _score(
    measure: str,
    value: float | None,
    *,
    threshold: str,
    passed: Any,
) -> dict[str, Any]:
    status = "INCONCLUSIVE" if value is None else "PASS" if passed(value) else "FAIL"
    return {
        "measure": measure,
        "value": value,
        "interval": None,
        "threshold": threshold,
        "status": status,
    }


def _finite_number(summary: dict[str, Any], key: str) -> float:
    value = float(summary[key])
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a replay report or record a strategy variant."
    )
    parser.add_argument(
        "report_dir",
        nargs="?",
        help="Directory containing summary.json and equity_curve.csv.",
    )
    parser.add_argument(
        "--cost-stress-report",
        help="Matching replay report created with --cost-multiplier 2.",
    )
    parser.add_argument("--record-variant", help="Unique identifier for a strategy variant tried.")
    parser.add_argument("--variant-description", help="Parameters/change description for the variant.")
    parser.add_argument(
        "--variant-ledger",
        default="data/reports/variants.json",
        help="Variant ledger path (default: data/reports/variants.json).",
    )
    args = parser.parse_args(argv)
    if args.record_variant is not None:
        if not args.variant_description:
            parser.error("--variant-description is required with --record-variant")
        if args.report_dir is not None or args.cost_stress_report is not None:
            parser.error("Variant recording cannot be combined with report analysis")
        try:
            count = record_variant(
                args.variant_ledger,
                args.record_variant,
                args.variant_description,
                recorded_at_utc=datetime.now(tz=UTC),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"Variant recording failed: {error}", file=sys.stderr)
            return 2
        print(f"Recorded strategy variant {args.record_variant}; total variants: {count}")
        return 0
    if args.report_dir is None:
        parser.error("report_dir is required unless --record-variant is used")
    try:
        path = write_scorecard(
            args.report_dir,
            cost_stress_report=args.cost_stress_report,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, csv.Error) as error:
        print(f"Statistics analysis failed: {error}", file=sys.stderr)
        return 2
    print(f"Saved replay scorecard: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
