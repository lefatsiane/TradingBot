from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount
from bot.app.config import (
    RiskSettings,
    build_simulator_costs,
    load_runtime_secrets,
    load_settings,
    parse_sessions,
    schedule_validation_failures,
    startup_guard,
)
from bot.app.logging_setup import configure_logging
from bot.app.versioning import get_version
from bot.core.gates import SessionWindow, next_weekend_close_utc


@pytest.mark.parametrize(
    ("field", "raised_value"),
    (
        ("risk_per_trade_pct", 0.51),
        ("daily_loss_pct", 1.01),
        ("monthly_loss_pct", 3.01),
        ("max_net_currency_exposure_pct", 1.01),
        ("max_open_trades", 4),
        ("cost_gate_pct_of_risk", 15.01),
        ("max_margin_used_pct", 30.01),
        ("cooldown_bars_after_stop", 7),
    ),
)
def test_risk_settings_reject_unapproved_limit_increases(
    field: str, raised_value: float
) -> None:
    approved_values = {
        "risk_per_trade_pct": 0.5,
        "daily_loss_pct": 1.0,
        "monthly_loss_pct": 3.0,
        "max_net_currency_exposure_pct": 1.0,
        "max_open_trades": 3,
        "cost_gate_pct_of_risk": 15.0,
        "max_margin_used_pct": 30.0,
        "cooldown_bars_after_stop": 6,
    }

    with pytest.raises(ValueError, match=f"{field} cannot exceed the approved limit"):
        RiskSettings.model_validate({**approved_values, field: raised_value})


def test_parse_sessions_accepts_valid_catalog_windows() -> None:
    sessions = parse_sessions(("mon 00:00-21:55", "fri 00:00-21:55"))

    assert sessions == (
        SessionWindow(0, time(0, 0), time(21, 55)),
        SessionWindow(4, time(0, 0), time(21, 55)),
    )


def test_enabled_symbols_require_non_overlapping_friday_schedules() -> None:
    source = Path(__file__).parents[1] / "settings.toml"
    settings, _ = load_settings(source)
    failures = schedule_validation_failures(settings)
    assert len(failures) == 6
    assert all("hours_utc is empty" in failure for failure in failures)
    assert not any("XAUUSD" in failure for failure in failures)

    symbols = {
        symbol: symbol_settings.model_copy(
            update={"hours_utc": ("mon 00:00-21:55", "fri 00:00-21:55")}
        )
        for symbol, symbol_settings in settings.symbols.items()
    }
    valid = settings.model_copy(update={"symbols": symbols})
    assert schedule_validation_failures(valid) == ()

    eurusd = symbols["EURUSD"].model_copy(
        update={
            "hours_utc": (
                "mon 00:00-12:00",
                "mon 11:00-15:00",
            )
        }
    )
    invalid = settings.model_copy(update={"symbols": {"EURUSD": eurusd}})
    issues = schedule_validation_failures(invalid)
    assert any("no Friday session" in issue for issue in issues)
    assert any("overlapping" in issue for issue in issues)


def test_next_weekend_close_utc_finds_the_next_friday_close() -> None:
    sessions = (
        SessionWindow(1, time(8, 0), time(17, 0)),
        SessionWindow(4, time(8, 0), time(23, 0)),
    )
    now = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)

    assert next_weekend_close_utc(now, sessions) == datetime(2026, 10, 30, 23, 0, tzinfo=UTC)


def test_next_weekend_close_utc_moves_to_following_friday_after_close() -> None:
    sessions = (SessionWindow(4, time(8, 0), time(17, 0)),)
    now = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)

    assert next_weekend_close_utc(now, sessions) == datetime(
        2026, 11, 6, 17, 0, tzinfo=UTC
    )


def test_runtime_secrets_come_only_from_environment() -> None:
    secrets = load_runtime_secrets(
        {
            "MT5_LOGIN": "123456",
            "MT5_PASSWORD": "demo-secret",
            "TELEGRAM_TOKEN": "telegram-secret",
            "HEALTHCHECK_URL": "https://example.invalid/ping",
        }
    )
    assert secrets.mt5_login == 123456
    assert secrets.mt5_password == "demo-secret"
    assert secrets.telegram_token == "telegram-secret"
    assert secrets.healthcheck_url == "https://example.invalid/ping"
    empty_secrets = load_runtime_secrets({})
    assert empty_secrets.mt5_login is None
    assert empty_secrets.mt5_password is None


@pytest.mark.parametrize("raw_login", ["", "abc", "0", "-1"])
def test_runtime_secrets_reject_invalid_login_environment_value(raw_login: str) -> None:
    if not raw_login:
        assert load_runtime_secrets({"MT5_LOGIN": raw_login}).mt5_login is None
        return
    with pytest.raises(ValueError, match="MT5_LOGIN must be a positive integer"):
        load_runtime_secrets({"MT5_LOGIN": raw_login})


def test_startup_guard_reports_account_and_terminal_mismatches() -> None:
    settings_path = Path(__file__).parents[1] / "settings.toml"
    settings, _ = load_settings(settings_path)
    account = BrokerAccount(
        login=settings.account.expected_login + 1,
        server="wrong-server",
        currency="USD",
        balance=3_000.0,
        equity=3_000.0,
        is_demo=False,
        trade_allowed=False,
        margin_mode="",
    )
    failures = startup_guard(
        settings,
        account,
        terminal_connected=False,
        terminal_trade_allowed=False,
    )
    assert len(failures) == 7
    assert "account login does not match expected_login" in failures
    assert "account type does not match configured mode" in failures


def test_settings_hash_is_stable_and_changes_with_configuration(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "settings.toml"
    text = source.read_text(encoding="utf-8")
    identical = tmp_path / "identical.toml"
    identical.write_text(text, encoding="utf-8")
    changed = tmp_path / "changed.toml"
    changed.write_text(text.replace("fast_tick_s = 2", "fast_tick_s = 1"), encoding="utf-8")

    _, original_hash = load_settings(source)
    _, identical_hash = load_settings(identical)
    _, changed_hash = load_settings(changed)

    assert identical_hash == original_hash
    assert changed_hash != original_hash


def test_settings_hash_does_not_include_account_login(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "settings.toml"
    text = source.read_text(encoding="utf-8")
    first = tmp_path / "first-login.toml"
    second = tmp_path / "second-login.toml"
    first.write_text(
        re.sub(r"(?m)^expected_login = \d+$", "expected_login = 123456", text),
        encoding="utf-8",
    )
    second.write_text(
        re.sub(r"(?m)^expected_login = \d+$", "expected_login = 654321", text),
        encoding="utf-8",
    )

    first_settings, first_hash = load_settings(first)
    second_settings, second_hash = load_settings(second)

    assert first_hash == second_hash
    assert first_settings.account.expected_login == 0
    assert second_settings.account.expected_login == 0


def test_simulator_costs_are_built_from_configured_symbol_settings() -> None:
    settings_path = Path(__file__).parents[1] / "settings.toml"
    settings, _ = load_settings(settings_path)

    costs = build_simulator_costs(settings, {"EURUSD", "GBPUSD"})

    assert set(costs) == {"EURUSD", "GBPUSD"}
    assert costs["EURUSD"].commission_per_lot_side == 3.5
    assert costs["EURUSD"].slippage_points == 2
    with pytest.raises(ValueError, match="no configured costs"):
        build_simulator_costs(settings, {"UNSUPPORTED"})


def test_settings_reject_unknown_keys(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "settings.toml"
    text = source.read_text(encoding="utf-8")
    invalid = tmp_path / "unknown-key.toml"
    invalid.write_text(text.replace("[account]", "[account]\nunknown_typo = true"), encoding="utf-8")

    with pytest.raises(ValueError):
        load_settings(invalid)


def test_versioning_reports_commit_and_dirty_tree(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "--quiet")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    source = tmp_path / "source.txt"
    source.write_text("committed\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "--quiet", "-m", "initial")

    version = get_version(tmp_path)
    assert len(version.commit) == 40
    assert version.clean

    source.write_text("uncommitted\n", encoding="utf-8")
    assert not get_version(tmp_path).clean


def test_versioning_reports_non_repository_as_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a git repository"):
        get_version(tmp_path)


def test_logging_setup_creates_rotating_log_file(tmp_path: Path) -> None:
    logger = configure_logging(tmp_path / "logs", logger_name="test.bot.logging")
    logger.info("test log entry")
    for handler in logger.handlers:
        handler.flush()
    log_path = tmp_path / "logs" / "bot.log"
    assert "test log entry" in log_path.read_text(encoding="utf-8")

    for handler in logger.handlers:
        handler.close()
        logger.removeHandler(handler)
