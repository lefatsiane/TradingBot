from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount
from bot.app.config import load_settings
from bot.app.data import DataQualityError
from bot.app.replay_runner import run_replay
from bot.core.types import Candle, SymbolSpec

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
        report_root=tmp_path,
    )

    assert result.news_enabled is False
    assert result.decisions
    assert result.ending_equity == 3_000.0
    assert result.deals == ()
    assert result.report_dir is not None
    assert (result.report_dir / "trades.csv").is_file()
    assert (result.report_dir / "equity_curve.csv").is_file()
    assert (result.report_dir / "summary.json").is_file()


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
