from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, BrokerTerminalInfo, Tick
from bot.adapters.sim_broker import SimBroker
from bot.app.config import (
    BrokerSessionHoursServerTime,
    RuntimeSecrets,
    SymbolSettings,
    load_settings,
)
from bot.app.symbol_validation import (
    SymbolValidationReport,
    _check_session_candles,
    validate_symbol,
    write_hours_utc,
)
from bot.core.clock import SimClock
from bot.core.types import Candle, SymbolSpec
from tools import validate_symbols as validate_symbols_tool

NOW = datetime(2026, 7, 7, 12, tzinfo=UTC)
SYMBOL = "EURUSD"
SESSIONS = (
    "sun 21:01-24:00",
    "mon 00:00-20:59",
    "mon 21:01-24:00",
    "tue 00:00-20:59",
    "tue 21:01-24:00",
    "wed 00:00-20:59",
    "wed 21:01-24:00",
    "thu 00:00-20:59",
    "thu 21:01-24:00",
    "fri 00:00-20:57",
)


def _broker(*, minimum_lot: float = 0.01) -> SimBroker:
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
    )


def _symbol_settings(*, enabled: bool = True) -> SymbolSettings:
    return SymbolSettings(
        enabled=enabled,
        commission_per_lot_side=0.01,
        slippage_points=0,
        max_spread_points=10,
        hours_utc=(),
    )


def test_validate_symbol_reports_server_hours_and_out_of_session_candles() -> None:
    report = validate_symbol(
        _broker(),
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert not report.passed
    assert report.history_bars >= 183 * 24
    assert report.suggested_hours_utc == SESSIONS
    assert report.suggested_hours_utc_season == "US summer"
    assert len(report.weekday_candle_times) == 7
    assert any("outside broker session hours" in issue for issue in report.issues)
    assert not any("session hour(s) have no candle" in issue for issue in report.issues)


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
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert not report.passed
    assert not report.enabled
    assert any("BELOW_MIN_LOT" in issue for issue in report.issues)
    assert settings.enabled is False


def test_session_candle_check_flags_outside_and_missing_expected_hours() -> None:
    start = datetime(2026, 7, 5, 23, tzinfo=UTC)
    candles = tuple(
        Candle(
            time=timestamp,
            open=1.1,
            high=1.10025,
            low=1.09975,
            close=1.1,
        )
        for timestamp in (
            start,
            datetime(2026, 7, 6, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 21, tzinfo=UTC),
        )
    )
    outside, missing = _check_session_candles(
        candles,
        BrokerSessionHoursServerTime(),
        cutoff=start - timedelta(hours=1),
        end_utc=datetime(2026, 7, 7, tzinfo=UTC),
    )

    assert datetime(2026, 7, 6, 21, tzinfo=UTC) in outside
    assert datetime(2026, 7, 6, 1, tzinfo=UTC) in missing


def test_disabled_symbol_does_not_receive_a_server_session_schedule() -> None:
    report = validate_symbol(
        _broker(),
        SYMBOL,
        _symbol_settings(enabled=False),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert not report.enabled
    assert report.suggested_hours_utc == ()
    assert report.suggested_hours_utc_season is None


def _prepare_validation_cli(monkeypatch, symbols: tuple[str, ...]):
    source = Path(__file__).parents[1] / "settings.toml"
    settings, _ = load_settings(source)
    settings = settings.model_copy(
        update={
            "account": settings.account.model_copy(update={"expected_server": "Demo"}),
            "symbols": {name: settings.symbols[name] for name in symbols},
        }
    )
    def load_for_test(path: Path):
        assert path.name == "settings.toml"
        return settings, "settings-hash"

    monkeypatch.setattr(validate_symbols_tool, "load_settings", load_for_test)
    monkeypatch.setattr(
        validate_symbols_tool,
        "load_runtime_secrets",
        lambda: RuntimeSecrets(
            123456,
            "demo-secret",
            "telegram-secret",
            "https://health-secret",
        ),
    )

    class FakeCliBroker:
        shutdown_called = False

        def connect(self, **kwargs) -> bool:
            assert set(kwargs) == {"login", "server"}
            return True

        def account(self) -> BrokerAccount:
            return BrokerAccount(
                123456,
                "Demo",
                "USD",
                3_000,
                3_000,
                True,
                True,
                "netting",
            )

        def terminal_info(self) -> BrokerTerminalInfo:
            return BrokerTerminalInfo(True, True, "Fake MT5", "Fake")

        def server_utc_offset_hours(self) -> int:
            return 2

        def shutdown(self) -> None:
            self.shutdown_called = True

    broker = FakeCliBroker()
    monkeypatch.setattr(validate_symbols_tool, "MT5Broker", lambda: broker)
    return broker


def test_validate_symbols_continues_after_failure_and_redacts_secrets(
    monkeypatch,
    capsys,
) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("EURUSD", "GBPUSD"))
    calls: list[str] = []

    def validate_one(broker_arg, symbol: str, symbol_settings, **kwargs):
        assert broker_arg is broker
        assert symbol_settings.enabled
        assert "server_session_hours" in kwargs
        calls.append(symbol)
        if symbol == "EURUSD":
            raise RuntimeError("failed with demo-secret")
        return SymbolValidationReport(
            symbol,
            True,
            True,
            4_000,
            1.0,
            ("sun 22:01-24:00",),
            "US winter",
            (),
        )

    monkeypatch.setattr(validate_symbols_tool, "validate_symbol", validate_one)

    assert validate_symbols_tool.main([]) == 1

    output = capsys.readouterr().out
    assert calls == ["EURUSD", "GBPUSD"]
    assert "GBPUSD" in output
    assert "EURUSD: validation raised RuntimeError" in output
    assert "demo-secret" not in output
    assert "telegram-secret" not in output
    assert "123456" not in output
    assert broker.shutdown_called


def test_validate_symbols_requires_review_confirmation_before_writing(
    monkeypatch,
    capsys,
) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("EURUSD",))

    def validate_one(broker_arg, symbol, symbol_settings, **kwargs):
        assert broker_arg is broker
        assert symbol_settings.enabled
        assert "server_session_hours" in kwargs
        return SymbolValidationReport(
            symbol,
            True,
            True,
            4_000,
            1.0,
            ("sun 22:01-24:00", "mon 00:00-21:59"),
            "US winter",
            (),
        )

    monkeypatch.setattr(
        validate_symbols_tool,
        "validate_symbol",
        validate_one,
    )
    writes: list[object] = []
    monkeypatch.setattr(
        validate_symbols_tool,
        "write_hours_utc",
        lambda *args: writes.append(args),
    )

    def decline_write(prompt: str) -> str:
        assert "APPLY" in prompt
        report_output = capsys.readouterr().out
        assert "SYMBOL  ENABLED" in report_output
        assert "suggested hours_utc" in report_output
        return "no"

    monkeypatch.setattr("builtins.input", decline_write)

    assert validate_symbols_tool.main(["--write"]) == 0

    assert writes == []
    assert "No settings were written." in capsys.readouterr().out
    assert broker.shutdown_called


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
    assert 'hours_utc = ["sun 21:01-24:00"' in updated
    assert 'hours_utc_season = "US summer"' in updated
    parsed = tomllib.loads(updated)
    assert tuple(parsed["symbols"][SYMBOL]["hours_utc"]) == SESSIONS
    assert parsed["symbols"][SYMBOL]["hours_utc_season"] == "US summer"
    assert parsed["risk"]["risk_per_trade_pct"] == 0.5
    assert parsed["strategy_a"]["ema_fast"] == 20

    write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US winter")
    rewritten = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    assert rewritten["symbols"][SYMBOL]["hours_utc_season"] == "US winter"
