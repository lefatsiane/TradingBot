from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.adapters.mt5_broker import MT5Broker
from bot.app.config import load_runtime_secrets, load_settings, schedule_season_for_utc
from bot.app.symbol_validation import SymbolValidationReport, validate_symbol, write_hours_utc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate configured symbol data and suggest UTC trading sessions."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write suggested hours_utc values and their reviewed season for passing symbols.",
    )
    args = parser.parse_args(argv)
    settings_path = Path(args.settings).resolve()
    try:
        settings, _ = load_settings(settings_path)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    broker = MT5Broker()
    connected = False
    try:
        secrets = load_runtime_secrets()
        if secrets.mt5_login is None:
            raise RuntimeError("MT5_LOGIN must be set in the environment")
        broker.connect(
            login=secrets.mt5_login,
            server=settings.account.expected_server,
        )
        connected = True
        account = broker.account()
        terminal = broker.terminal_info()
        account_failures: list[str] = []
        if account.login != secrets.mt5_login:
            account_failures.append("account login does not match MT5_LOGIN")
        if account.server != settings.account.expected_server:
            account_failures.append("account server does not match expected_server")
        if not account.is_demo:
            account_failures.append("account is not a demo account")
        if not terminal.connected:
            account_failures.append("MT5 terminal is disconnected")
        if account_failures:
            print("Account validation failed: " + "; ".join(account_failures), file=sys.stderr)
            return 2

        now_utc = datetime.now(tz=UTC)
        reports = [
            validate_symbol(
                broker,
                symbol,
                symbol_settings,
                equity=account.equity,
                risk_per_trade_pct=settings.risk.risk_per_trade_pct,
                cost_gate_pct_of_risk=settings.risk.cost_gate_pct_of_risk,
                max_stop_atr=max(
                    settings.strategy_a.max_stop_atr,
                    settings.strategy_b.max_stop_atr,
                ),
                now_utc=now_utc,
            )
            for symbol, symbol_settings in settings.symbols.items()
        ]
    except (ConnectionError, OSError, PermissionError, RuntimeError, ValueError) as error:
        print(f"Symbol validation could not run: {error}", file=sys.stderr)
        return 2
    finally:
        if connected:
            broker.shutdown()

    _print_reports(reports)
    if args.write:
        passing_schedules = {
            report.symbol: report.suggested_hours_utc
            for report in reports
            if report.passed
        }
        try:
            write_hours_utc(
                settings_path,
                passing_schedules,
                schedule_season_for_utc(now_utc),
            )
        except (OSError, ValueError) as error:
            print(f"Could not write UTC schedules: {error}", file=sys.stderr)
            return 2
        print(f"Wrote UTC hours for {len(passing_schedules)} passing symbols.")

    enabled_failures = [
        report for report in reports if report.enabled and not report.passed
    ]
    if enabled_failures:
        return 1
    return 0


def _print_reports(reports: list[SymbolValidationReport]) -> None:
    print("SYMBOL  ENABLED  STATUS  H1 BARS  MAX SPREAD  UTC SESSIONS")
    for report in reports:
        enabled = "yes" if report.enabled else "no"
        status = "PASS" if report.passed else "FAIL"
        spread = (
            "n/a"
            if report.max_observed_spread_points is None
            else f"{report.max_observed_spread_points:.2f} pt"
        )
        sessions = "; ".join(report.suggested_hours_utc) or "n/a"
        print(
            f"{report.symbol:<7} {enabled:<8} {status:<7} "
            f"{report.history_bars:<8} {spread:<11} {sessions}"
        )
        for issue in report.issues:
            print(f"  - {issue}")
        if report.suggested_hours_utc:
            print(
                f"  suggested hours_utc = {report.suggested_hours_utc} "
                f"(reviewed in {report.suggested_hours_utc_season})"
            )


if __name__ == "__main__":
    raise SystemExit(main())
