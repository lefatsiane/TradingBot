from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.adapters.mt5_broker import MT5Broker
from bot.app.config import load_runtime_secrets, load_settings, schedule_season_for_utc
from bot.app.symbol_validation import (
    SymbolValidationReport,
    TickFreshnessDiagnostic,
    _market_is_open,
    sample_live_spread,
    validate_symbol,
    write_hours_utc,
)
from bot.core.timeutil import broker_wall_time_to_utc, expected_new_york_offset

EXIT_ROLLOVER_WINDOW = 3
EXIT_WEEKEND_CLOSED = 4
EXIT_MARKET_CLOSED = 5


def _validation_period(settings, now_utc: datetime) -> str | None:
    timing = settings.timing
    server_date = (
        now_utc + timedelta(hours=expected_new_york_offset(now_utc))
    ).date()
    boundaries = (
        broker_wall_time_to_utc(
            datetime.combine(server_date + timedelta(days=day_delta), time.min)
            .replace(tzinfo=UTC)
            .timestamp()
        )
        for day_delta in (-1, 0, 1)
    )
    for boundary in boundaries:
        if (
            boundary - timedelta(minutes=timing.rollover_block_before_min)
            <= now_utc
            <= boundary + timedelta(minutes=timing.rollover_block_after_min)
        ):
            return "rollover"
    if (now_utc + timedelta(hours=expected_new_york_offset(now_utc))).weekday() >= 5:
        return "weekend"
    if not _market_is_open(now_utc, settings.broker_session_hours_server_time):
        return "market_closed"
    return None


def _offset_failure_reason(error: Exception) -> str:
    message = str(error).casefold()
    if "stale" in message or "too old" in message:
        return "tick too stale"
    if (
        "did not advance" in message
        or "no fresh tick" in message
        or "no valid time_msc" in message
    ):
        return "no fresh tick"
    if "offset" in message or "reports utc" in message or "expects utc" in message:
        return "offset mismatch"
    return f"offset verification unavailable ({type(error).__name__})"


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
        if args.write:
            print(
                f"--write not performed: settings could not be loaded "
                f"({type(error).__name__})."
            )
        return 2

    broker = MT5Broker()
    connected = False
    account_is_demo = False
    reports: list[SymbolValidationReport] = []
    failures: list[str] = []
    secrets = None
    offset_message = "Broker UTC offset: not checked"
    now_utc = datetime.now(tz=UTC)
    validation_period: str | None = None
    write_outcome: str | None = None
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
        account_is_demo = account.is_demo
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
            validation_period = _validation_period(settings, now_utc)
            if validation_period == "rollover":
                offset_message = (
                    "rollover window: spread and offset not evaluated, rerun later"
                )
            elif validation_period == "weekend":
                offset_message = (
                    "weekend: spread and offset not evaluated, rerun when market opens"
                )
            elif validation_period == "market_closed":
                offset_message = (
                    "market closed: spread and offset not evaluated, rerun when market opens"
                )
            else:
                try:
                    offset = broker.server_utc_offset_hours()
                    offset_message = (
                        f"Broker UTC offset: UTC{offset:+d}, verified against fresh EURUSD ticks"
                    )
                except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                    reason = _offset_failure_reason(error)
                    failures.append(f"Broker UTC offset check failed: {reason}")
                    offset_message = failures[-1]

        if not account_failures and validation_period is None:
            for symbol, symbol_settings in settings.symbols.items():
                try:
                    live_spread_samples: tuple[float, ...] | None = None
                    live_spread_errors: tuple[str, ...] = ()
                    if symbol_settings.enabled:
                        try:
                            spec = broker.symbol_info(symbol)
                            sample_report = sample_live_spread(
                                broker,
                                symbol,
                                spec.point,
                            )
                            live_spread_samples = sample_report.points
                            live_spread_errors = sample_report.errors
                        except (OSError, RuntimeError, ValueError) as error:
                            live_spread_samples = ()
                            live_spread_errors = (
                                f"live spread sampling unavailable ({type(error).__name__})",
                            )
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
                            now_utc=datetime.now(tz=UTC),
                            server_session_hours=settings.broker_session_hours_server_time,
                            live_spread_samples=live_spread_samples,
                            live_spread_errors=live_spread_errors,
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolate each symbol and report every failure
                    reports.append(
                        SymbolValidationReport(
                            symbol=symbol,
                            enabled=symbol_settings.enabled,
                            passed=False,
                            history_bars=0,
                            spread_median_points=None,
                            spread_p95_points=None,
                            current_spread_points=None,
                            spread_limit_points=symbol_settings.max_spread_points,
                            suggested_hours_utc=(),
                            suggested_hours_utc_season=None,
                            issues=(f"validation raised {type(error).__name__}",),
                        )
                    )
    except (ConnectionError, OSError, PermissionError, RuntimeError, ValueError) as error:
        print(f"Symbol validation could not run ({type(error).__name__}).", file=sys.stderr)
        if args.write:
            print(
                f"--write not performed: MT5 connection/account validation failed "
                f"({type(error).__name__})."
            )
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
    if args.write:
        if validation_period is not None:
            write_outcome = f"--write not performed: {offset_message}."
        elif not account_is_demo:
            write_outcome = (
                "--write not performed: the connected account is not a demo account."
            )
        elif failures:
            write_outcome = (
                "--write not performed: global validation failed: "
                + "; ".join(failures)
                + "."
            )
        else:
            passing_schedules = {
                report.symbol: report.suggested_hours_utc
                for report in reports
                if (
                    report.enabled
                    and report.passed
                    and report.suggested_hours_utc
                )
            }
            if passing_schedules:
                try:
                    confirmation = input(
                        "Review the report above. Type APPLY to update settings.toml: "
                    )
                except EOFError:
                    confirmation = ""
                if confirmation != "APPLY":
                    write_outcome = (
                        "--write not performed: report confirmation was not provided."
                    )
                else:
                    try:
                        changed_lines = write_hours_utc(
                            settings_path,
                            passing_schedules,
                            schedule_season_for_utc(now_utc),
                        )
                    except (OSError, ValueError) as error:
                        write_outcome = (
                            "--write not performed: settings write failed "
                            f"({type(error).__name__})."
                        )
                    else:
                        if changed_lines:
                            changed_symbols = ", ".join(changed_lines)
                            for symbol, written_lines in changed_lines.items():
                                print(f"settings.toml [symbols.{symbol}]")
                                for line in written_lines:
                                    print(line)
                            write_outcome = (
                                f"--write wrote symbols {changed_symbols} "
                                f"to {settings_path}."
                            )
                        else:
                            write_outcome = (
                                "--write not performed: settings already contain "
                                "the requested schedules."
                            )
            else:
                failed_symbols = [
                    report.symbol
                    for report in reports
                    if report.enabled and not report.passed
                ]
                disabled_symbols = [
                    report.symbol for report in reports if not report.enabled
                ]
                reasons = []
                if failed_symbols:
                    reasons.append("FAIL symbols: " + ", ".join(failed_symbols))
                if disabled_symbols:
                    reasons.append("DISABLED symbols excluded: " + ", ".join(disabled_symbols))
                write_outcome = (
                    "--write not performed: no enabled PASS or WARN symbol has a "
                    "schedule to write"
                    + (f" ({'; '.join(reasons)})" if reasons else "")
                    + "."
                )

    enabled_failures = [
        report for report in reports if report.enabled and not report.passed
    ]
    print("\nDATA GAPS")
    data_gap_reports = [report for report in reports if report.data_gaps]
    if data_gap_reports:
        for report in data_gap_reports:
            for gap in report.data_gaps:
                print(f"- {report.symbol}: {_format_datetime(gap)} UTC")
    else:
        print("None")
    print("\nFAILURES")
    if failures or enabled_failures:
        for failure in failures:
            print(f"- {_redact(failure, sensitive_values)}")
        for report in enabled_failures:
            for issue in report.issues or ("validation failed",):
                print(f"- {report.symbol}: {_redact(issue, sensitive_values)}")
    else:
        print("None")
    if args.write and write_outcome is not None:
        print(write_outcome)
    if validation_period == "rollover":
        return EXIT_ROLLOVER_WINDOW
    if validation_period == "weekend":
        return EXIT_WEEKEND_CLOSED
    if validation_period == "market_closed":
        return EXIT_MARKET_CLOSED
    if enabled_failures:
        return 1
    return 1 if failures else 0


def _print_reports(
    reports: list[SymbolValidationReport],
    sensitive_values: tuple[str, ...] = (),
) -> None:
    print(
        "SYMBOL  ENABLED  STATUS  H1 BARS  RECORDED IN CANDLES MEDIAN/P95  "
        "LIVE MEDIAN/MAX/CURRENT (N)  LAST CLOSED AGE  UTC SESSIONS"
    )
    for report in reports:
        enabled = "yes" if report.enabled else "no"
        if not report.enabled:
            status = "DISABLED"
        elif not report.passed:
            status = "FAIL"
        elif report.data_gaps:
            status = "WARN"
        else:
            status = "PASS"
        spread = (
            f"{_format_points(report.spread_median_points)} / "
            f"{_format_points(report.spread_p95_points)}"
        )
        live_spread = (
            f"{_format_points(report.live_spread_median_points)} / "
            f"{_format_points(report.live_spread_max_points)} "
            f"/ {_format_points(report.current_spread_points)} "
            f"({report.live_spread_sample_count}) "
            f"(limit {report.spread_limit_points:.2f})"
        )
        last_age = (
            "n/a"
            if report.last_closed_candle_age_hours is None
            else f"{report.last_closed_candle_age_hours:.1f} h"
        )
        sessions = "; ".join(report.suggested_hours_utc) or "n/a"
        print(
            f"{report.symbol:<7} {enabled:<8} {status:<7} {report.history_bars:<8} "
            f"{spread:<29} {live_spread:<38} {last_age:<16} {sessions}"
        )
        if report.enabled and report.tick_freshness is not None:
            _print_tick_diagnostic(report.symbol, report.tick_freshness)
        elif report.enabled:
            print(f"  {report.symbol} tick diagnostic unavailable")
        if report.tick_freshness is not None and not report.tick_freshness.market_open:
            print("  market closed; tick freshness is not required")
        for gap in report.data_gaps:
            print(f"  DATA GAP: {_format_datetime(gap)} UTC")
        for summary in report.weekday_candle_times:
            first = _format_datetime(summary.first_utc)
            last = _format_datetime(summary.last_utc)
            print(f"  {summary.weekday}: first {first} UTC; last {last} UTC")
        for issue in report.issues:
            print(f"  - {_redact(issue, sensitive_values)}")
        if report.suggested_hours_utc:
            print(
                f"  suggested hours_utc = {report.suggested_hours_utc} "
                f"(reviewed in {report.suggested_hours_utc_season})"
            )


def _format_points(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} pt"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "none"
    return f"{value:%Y-%m-%d} {value:%H:%M}"


def _print_tick_diagnostic(
    symbol: str,
    diagnostic: TickFreshnessDiagnostic,
) -> None:
    raw_tick = (
        "unavailable"
        if diagnostic.raw_server_epoch is None
        else datetime.fromtimestamp(
            diagnostic.raw_server_epoch,
            tz=UTC,
        ).strftime("%Y-%m-%d %H:%M:%S")
    )
    converted = _format_datetime(diagnostic.converted_time_utc)
    age = (
        "unavailable"
        if diagnostic.age_seconds is None
        else f"{diagnostic.age_seconds:.1f} s"
    )
    print(f"  {symbol} tick diagnostic:")
    print(f"    raw tick time (server clock): {raw_tick}")
    print(f"    tick time converted to UTC: {converted}")
    print(f"    current UTC time: {_format_datetime(diagnostic.current_time_utc)}")
    print(f"    age: {age}; threshold: {diagnostic.threshold_seconds:.0f} s")
    bid = "unavailable" if diagnostic.bid is None else f"{diagnostic.bid:.8f}"
    ask = "unavailable" if diagnostic.ask is None else f"{diagnostic.ask:.8f}"
    print(f"    bid: {bid}; ask: {ask}")
    if diagnostic.invalid_reason is None:
        print("    tick validity: valid")
    else:
        print(f"    tick validity: invalid ({diagnostic.invalid_reason})")
    if diagnostic.clock_skew_seconds is not None and diagnostic.clock_skew_seconds > 0:
        if diagnostic.clock_skew_seconds > 30:
            print(
                "    WARNING: tick clock skew "
                f"{diagnostic.clock_skew_seconds:.1f} s (tick ahead)"
            )
        else:
            print(
                f"    tick clock skew: {diagnostic.clock_skew_seconds:.1f} s (tick ahead)"
            )


def _redact(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        redacted = redacted.replace(sensitive, "[REDACTED]")
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
