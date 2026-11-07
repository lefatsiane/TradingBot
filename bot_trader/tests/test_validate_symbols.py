from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, Tick
from bot.adapters.sim_broker import SimBroker
from bot.app.config import SymbolSettings
from bot.app.symbol_validation import validate_symbol, write_hours_utc
from bot.core.clock import SimClock
from bot.core.types import Candle, SymbolSpec

NOW = datetime(2026, 7, 7, 12, tzinfo=UTC)
SYMBOL = "EURUSD"
SESSIONS = (
    "mon 00:00-21:55",
    "tue 00:00-21:55",
    "wed 00:00-21:55",
    "thu 00:00-21:55",
    "fri 00:00-21:55",
)


def _broker(*, minimum_lot: float = 0.01, sessions: tuple[str, ...] = SESSIONS) -> SimBroker:
    start = NOW - timedelta(days=183)
    bars = tuple(
        Candle(
            time=start + timedelta(hours=index),
            open=1.1,
            high=1.10025,
            low=1.09975,
            close=1.1,
            spread=0.00005,
        )
        for index in range(183 * 24 + 1)
    )
    spec = SymbolSpec(
        name=SYMBOL,
        base_currency="EUR",
        quote_currency="USD",
        point=0.00001,
        tick_size=0.0001,
        tick_value_loss=10.0,
        volume_min=minimum_lot,
        volume_step=minimum_lot,
        volume_max=100.0,
        digits=5,
        contract_size=100_000.0,
    )
    return SimBroker(
        account_snapshot=BrokerAccount(
            login=123,
            server="Demo",
            currency="USD",
            balance=3_000.0,
            equity=3_000.0,
            is_demo=True,
            trade_allowed=True,
            margin_mode="netting",
        ),
        specs={SYMBOL: spec},
        tick_data={SYMBOL: Tick(NOW, 1.1, 1.10005)},
        clock=SimClock(NOW),
        candle_data={(SYMBOL, "H1"): bars},
        session_hours={SYMBOL: sessions},
    )


def _symbol_settings(*, enabled: bool = True) -> SymbolSettings:
    return SymbolSettings(
        enabled=enabled,
        commission_per_lot_side=0.01,
        slippage_points=0,
        max_spread_points=10,
        hours_utc=(),
    )


def test_validate_symbol_passes_with_six_months_of_simulated_history() -> None:
    report = validate_symbol(
        _broker(),
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
    )

    assert report.passed
    assert report.issues == ()
    assert report.history_bars >= 183 * 24
    assert report.suggested_hours_utc == SESSIONS
    assert report.suggested_hours_utc_season == "US summer"


def test_oversized_minimum_lot_reports_below_min_lot_without_enabling_symbol() -> None:
    settings = _symbol_settings(enabled=False)
    report = validate_symbol(
        _broker(minimum_lot=1.0),
        SYMBOL,
        settings,
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
    )

    assert not report.passed
    assert not report.enabled
    assert any("BELOW_MIN_LOT" in issue for issue in report.issues)
    assert settings.enabled is False


def test_validate_symbol_rejects_overlapping_or_missing_friday_sessions() -> None:
    sessions = (
        "mon 00:00-12:00",
        "mon 11:00-21:55",
        "tue 00:00-21:55",
    )
    report = validate_symbol(
        _broker(sessions=sessions),
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
    )

    assert not report.passed
    assert any("overlap" in issue.lower() for issue in report.issues)
    assert any("Friday" in issue for issue in report.issues)


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_write_hours_utc_changes_only_symbol_hours(
    tmp_path: Path,
    line_ending: str,
) -> None:
    settings_path = tmp_path / "settings.toml"
    original = line_ending.join(
        (
            "[risk]",
            "risk_per_trade_pct = 0.5",
            "[strategy_a]",
            "ema_fast = 20",
            "[symbols.EURUSD]",
            "enabled = true",
            "hours_utc = []",
            "",
        )
    )
    with settings_path.open("w", encoding="utf-8", newline="") as settings_file:
        settings_file.write(original)

    write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US summer")

    updated = settings_path.read_bytes().decode("utf-8")
    assert line_ending in updated
    if line_ending == "\n":
        assert "\r\n" not in updated
    else:
        assert "\n" not in updated.replace("\r\n", "")
    assert [
        line
        for line in updated.splitlines()
        if not line.startswith(("hours_utc", "hours_utc_season"))
    ] == [
        line
        for line in original.splitlines()
        if not line.startswith(("hours_utc", "hours_utc_season"))
    ]
    assert 'hours_utc = ["mon 00:00-21:55"' in updated
    assert 'hours_utc_season = "US summer"' in updated
    parsed = tomllib.loads(updated)
    assert tuple(parsed["symbols"][SYMBOL]["hours_utc"]) == SESSIONS
    assert parsed["symbols"][SYMBOL]["hours_utc_season"] == "US summer"
    assert parsed["risk"]["risk_per_trade_pct"] == 0.5
    assert parsed["strategy_a"]["ema_fast"] == 20

    write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US winter")
    rewritten = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    assert rewritten["symbols"][SYMBOL]["hours_utc_season"] == "US winter"
