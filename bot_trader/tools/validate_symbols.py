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
        description="Audit symbol H1 candles and a fresh-tick offset against server-time sessions."
    )
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument(
        "--write",
        action="store_true",
        help="After report review and confirmation, write passing UTC hours and their season.",
    )
    args = parser.parse_args(argv)
    settings_path = Path(args.settings).resolve()
    try:
        settings, _ = load_settings(settings_path)
    except (OSError, ValueError) as error:
        print(f"Configuration error ({type(error).__name__}).", file=sys.stderr)
        return 2

    broker = MT5Broker()
    connected = False
    reports: list[SymbolValidationReport] = []
    failures: list[str] = []
    secrets = None
    offset_message = "Broker UTC offset: not checked"
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
            failures.extend(account_failures)
        else:
            now_utc = datetime.now(tz=UTC)
            try:
                offset = broker.server_utc_offset_hours()
                offset_message = (
                    f"Broker UTC offset: UTC{offset:+d}, verified against fresh EURUSD ticks"
                )
            except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                failures.append(
                    f"Broker UTC offset check failed ({type(error).__name__})"
                )
                offset_message = failures[-1]

        if not account_failures:
            for symbol, symbol_settings in settings.symbols.items():
                try:
                    reports.append(
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
                            server_session_hours=settings.broker_session_hours_server_time,
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolate each symbol and report every failure
                    reports.append(
                        SymbolValidationReport(
                            symbol=symbol,
                            enabled=symbol_settings.enabled,
                            passed=False,
                            history_bars=0,
                            max_observed_spread_points=None,
                            suggested_hours_utc=(),
                            suggested_hours_utc_season=None,
                            issues=(f"validation raised {type(error).__name__}",),
                        )
                    )
    except (ConnectionError, OSError, PermissionError, RuntimeError, ValueError) as error:
        print(f"Symbol validation could not run ({type(error).__name__}).", file=sys.stderr)
        return 2
    finally:
        if connected:
            try:
                broker.shutdown()
            except Exception as error:  # noqa: BLE001 - report shutdown failures without credentials
                failures.append(
                    f"MT5 shutdown failed ({type(error).__name__})"
                )

    sensitive_values = ()
    if secrets is not None:
        sensitive_values = tuple(
            value
            for value in (
                str(secrets.mt5_login) if secrets.mt5_login is not None else None,
                secrets.mt5_password,
                secrets.telegram_token,
                secrets.healthcheck_url,
            )
            if value
        )
    print(offset_message)
    _print_reports(reports, sensitive_values)
    if args.write and not failures:
        passing_schedules = {
            report.symbol: report.suggested_hours_utc
            for report in reports
            if report.enabled and report.passed and report.suggested_hours_utc
        }
        if passing_schedules:
            try:
                confirmation = input(
                    "Review the report above. Type APPLY to update settings.toml: "
                )
            except EOFError:
                confirmation = ""
            if confirmation == "APPLY":
                try:
                    write_hours_utc(
                        settings_path,
                        passing_schedules,
                        schedule_season_for_utc(now_utc),
                    )
                except (OSError, ValueError) as error:
                    failures.append(
                        f"Could not write UTC schedules ({type(error).__name__})"
                    )
                else:
                    print(f"Wrote UTC hours for {len(passing_schedules)} passing symbols.")
            else:
                print("No settings were written.")
        else:
            print("No passing enabled-symbol schedules are available to write.")

    failed_reports = [report for report in reports if not report.passed]
    enabled_failures = [report for report in failed_reports if report.enabled]
    print("\nFAILURES")
    if failures or failed_reports:
        for failure in failures:
            print(f"- {_redact(failure, sensitive_values)}")
        for report in failed_reports:
            for issue in report.issues:
                print(f"- {report.symbol}: {_redact(issue, sensitive_values)}")
    else:
        print("None")
    if enabled_failures:
        return 1
    return 1 if failures else 0


def _print_reports(
    reports: list[SymbolValidationReport],
    sensitive_values: tuple[str, ...] = (),
) -> None:
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
        for summary in report.weekday_candle_times:
            first = (
                "none"
                if summary.first_utc is None
                else summary.first_utc.strftime("%Y-%m-%d %H:%M")
            )
            last = (
                "none"
                if summary.last_utc is None
                else summary.last_utc.strftime("%Y-%m-%d %H:%M")
            )
            print(f"  {summary.weekday}: first {first} UTC; last {last} UTC")
        for issue in report.issues:
            print(f"  - {_redact(issue, sensitive_values)}")
        if report.suggested_hours_utc:
            print(
                f"  suggested hours_utc = {report.suggested_hours_utc} "
                f"(reviewed in {report.suggested_hours_utc_season})"
            )


def _redact(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        redacted = redacted.replace(sensitive, "[REDACTED]")
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
