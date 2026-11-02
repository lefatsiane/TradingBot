from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount
from bot.app.config import (
    build_simulator_costs,
    load_runtime_secrets,
    load_settings,
    startup_guard,
)
from bot.app.logging_setup import configure_logging
from bot.app.versioning import get_version


def test_runtime_secrets_come_only_from_environment() -> None:
    secrets = load_runtime_secrets(
        {
            "MT5_PASSWORD": "demo-secret",
            "TELEGRAM_TOKEN": "telegram-secret",
            "HEALTHCHECK_URL": "https://example.invalid/ping",
        }
    )
    assert secrets.mt5_password == "demo-secret"
    assert secrets.telegram_token == "telegram-secret"
    assert secrets.healthcheck_url == "https://example.invalid/ping"
    assert load_runtime_secrets({}).mt5_password is None


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
