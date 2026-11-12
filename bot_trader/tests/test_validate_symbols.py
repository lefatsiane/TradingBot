from __future__ import annotations

import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.adapters.broker import BrokerAccount, BrokerTerminalInfo, Tick
from bot.adapters.sim_broker import SimBroker
from bot.app import symbol_validation
from bot.app.config import (
    BrokerSessionHoursServerTime,
    RuntimeSecrets,
    SymbolSettings,
    load_settings,
)
from bot.app.symbol_validation import (
    LiveSpreadSamples,
    SymbolValidationReport,
    TickFreshnessDiagnostic,
    _check_session_candles,
    _historical_spread_statistics,
    _history_start_is_stale,
    _market_open_duration,
    _tick_invalid_reason,
    sample_live_spread,
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

    assert datetime(2026, 7, 6, 21, tzinfo=UTC) not in outside
    assert datetime(2026, 7, 6, 1, tzinfo=UTC) in missing


def test_session_check_uses_date_specific_dst_for_candles_around_clock_changes() -> None:
    timestamps = (
        datetime(2026, 3, 6, 20, tzinfo=UTC),
        datetime(2026, 3, 8, 21, tzinfo=UTC),
        datetime(2026, 3, 9, 21, tzinfo=UTC),
        datetime(2026, 10, 30, 20, tzinfo=UTC),
        datetime(2026, 11, 1, 22, tzinfo=UTC),
        datetime(2026, 11, 2, 22, tzinfo=UTC),
    )
    candles = tuple(
        Candle(timestamp, 1.1, 1.1001, 1.0999, 1.1)
        for timestamp in timestamps
    )

    outside, _ = _check_session_candles(
        candles,
        BrokerSessionHoursServerTime(),
        cutoff=timestamps[0] - timedelta(days=1),
        end_utc=timestamps[-1] + timedelta(hours=1),
    )

    assert outside == ()


def test_session_check_reports_the_june_29_0600_utc_candle_gap() -> None:
    candles = tuple(
        Candle(timestamp, 1.1, 1.1001, 1.0999, 1.1)
        for timestamp in (
            datetime(2026, 6, 29, 5, tzinfo=UTC),
            datetime(2026, 6, 29, 7, tzinfo=UTC),
        )
    )

    _, missing = _check_session_candles(
        candles,
        BrokerSessionHoursServerTime(),
        cutoff=datetime(2026, 6, 29, 4, tzinfo=UTC),
        end_utc=datetime(2026, 6, 29, 8, tzinfo=UTC),
    )

    assert datetime(2026, 6, 29, 6, tzinfo=UTC) in missing


def test_session_check_does_not_expect_the_current_forming_h1_bar() -> None:
    current_bar = datetime(2026, 7, 5, 21, tzinfo=UTC)
    candles = (Candle(current_bar, 1.1, 1.1001, 1.0999, 1.1),)

    _, missing = _check_session_candles(
        candles,
        BrokerSessionHoursServerTime(),
        cutoff=current_bar - timedelta(hours=1),
        end_utc=current_bar + timedelta(minutes=30),
    )

    assert missing == ()


def test_historical_spreads_exclude_rollover_and_week_edges() -> None:
    times_and_spreads = (
        (datetime(2026, 6, 1, 8, tzinfo=UTC), 1.0),
        (datetime(2026, 6, 1, 9, tzinfo=UTC), 2.0),
        (datetime(2026, 6, 1, 10, tzinfo=UTC), 100.0),
        (datetime(2026, 6, 1, 20, tzinfo=UTC), 1_000.0),
        (datetime(2026, 6, 1, 21, tzinfo=UTC), 10_000.0),
        (datetime(2026, 5, 30, 21, tzinfo=UTC), 20_000.0),
    )
    candles = tuple(
        Candle(timestamp, 1.1, 1.1001, 1.0999, 1.1, spread)
        for timestamp, spread in times_and_spreads
    )

    assert _historical_spread_statistics(candles, point=1.0) == (2.0, 100.0)


def test_history_start_accepts_a_weekend_gap_but_not_more_than_three_days() -> None:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    assert not _history_start_is_stale(cutoff + timedelta(days=3), cutoff)
    assert _history_start_is_stale(
        cutoff + timedelta(days=3, seconds=1),
        cutoff,
    )


def test_history_staleness_counts_scheduled_open_time_not_closed_weekend() -> None:
    open_intervals = _market_open_duration(
        datetime(2026, 1, 9, 21, 57, tzinfo=UTC),
        datetime(2026, 1, 11, 22, 1, tzinfo=UTC),
        BrokerSessionHoursServerTime(),
    )

    assert open_intervals == timedelta(0)


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


def test_market_closed_tick_does_not_fail_symbol_validation() -> None:
    closed_now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    broker = _broker()
    broker.tick_data[SYMBOL] = Tick(closed_now - timedelta(hours=2), 1.1, 1.10005)

    report = validate_symbol(
        broker,
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=closed_now,
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert report.tick_freshness is not None
    assert not report.tick_freshness.market_open
    assert not any("tick" in issue.lower() for issue in report.issues)


def test_tick_allows_small_future_clock_skew_and_reports_tick_condition() -> None:
    future_tick = Tick(NOW + timedelta(seconds=4.3), 1.1, 1.10005)

    assert _tick_invalid_reason(future_tick, NOW) is None
    assert _tick_invalid_reason(
        Tick(NOW + timedelta(seconds=30.1), 1.1, 1.10005),
        NOW,
    ) == "tick timestamp is 30.1 seconds in the future"

    broker = _broker()
    broker.tick_data[SYMBOL] = future_tick
    report = validate_symbol(
        broker,
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert report.tick_freshness is not None
    assert report.tick_freshness.invalid_reason is None
    assert report.tick_freshness.clock_skew_seconds == pytest.approx(4.3)
    assert report.tick_freshness.bid == pytest.approx(1.1)
    assert report.tick_freshness.ask == pytest.approx(1.10005)
    assert not any("invalid tick" in issue for issue in report.issues)


def test_live_spread_sampler_uses_fresh_ticks_for_about_one_minute() -> None:
    broker = _broker()
    elapsed = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return elapsed

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    result = sample_live_spread(
        broker,
        SYMBOL,
        0.00001,
        duration_seconds=10,
        interval_seconds=5,
        clock=lambda: NOW,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert result.points == pytest.approx((5.0, 5.0, 5.0))
    assert sleeps == [5, 5]


def test_live_median_controls_spread_gate_not_candle_spread() -> None:
    broker = _broker()
    low_limit = _symbol_settings().model_copy(update={"max_spread_points": 1})
    candle_spread_report = validate_symbol(
        broker,
        SYMBOL,
        low_limit,
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
        live_spread_samples=(1.0, 1.0),
    )
    live_spread_report = validate_symbol(
        broker,
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
        live_spread_samples=(11.0, 15.0),
    )

    assert not any("spread exceeds" in issue for issue in candle_spread_report.issues)
    assert candle_spread_report.live_spread_median_points == 1.0
    assert live_spread_report.live_spread_median_points == 13.0
    assert live_spread_report.live_spread_max_points == 15.0
    assert any("live median spread 13.00" in issue for issue in live_spread_report.issues)


def test_stale_open_market_history_retries_once_and_reports_age(monkeypatch) -> None:
    broker = _broker()
    broker.candle_data[(SYMBOL, "H1")] = tuple(
        candle
        for candle in broker.candle_data[(SYMBOL, "H1")]
        if candle.time <= NOW - timedelta(hours=3)
    )
    original_candles = type(broker).candles
    history_calls = 0
    delays: list[float] = []

    def count_history_calls(self, symbol: str, timeframe: str):
        nonlocal history_calls
        history_calls += 1
        return original_candles(self, symbol, timeframe)

    monkeypatch.setattr(type(broker), "candles", count_history_calls)
    monkeypatch.setattr(
        symbol_validation.time_module,
        "sleep",
        lambda seconds: delays.append(seconds),
    )

    report = validate_symbol(
        broker,
        SYMBOL,
        _symbol_settings(),
        equity=3_000,
        risk_per_trade_pct=0.5,
        cost_gate_pct_of_risk=15,
        max_stop_atr=3,
        now_utc=NOW,
        server_session_hours=BrokerSessionHoursServerTime(),
    )

    assert history_calls == 2
    assert delays == [1.0]
    assert any(
        "H1 history is not up to date" in issue and "2.0 hours" in issue
        for issue in report.issues
    )


def _prepare_validation_cli(
    monkeypatch,
    symbols: tuple[str, ...],
    *,
    is_demo: bool = True,
):
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
                is_demo,
                True,
                "netting",
            )

        def terminal_info(self) -> BrokerTerminalInfo:
            return BrokerTerminalInfo(True, True, "Fake MT5", "Fake")

        def symbol_info(self, symbol: str) -> SymbolSpec:
            return SymbolSpec(
                symbol,
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

        def server_utc_offset_hours(self) -> int:
            return 2

        def shutdown(self) -> None:
            self.shutdown_called = True

    broker = FakeCliBroker()
    monkeypatch.setattr(validate_symbols_tool, "MT5Broker", lambda: broker)
    monkeypatch.setattr(
        validate_symbols_tool,
        "sample_live_spread",
        lambda *args: LiveSpreadSamples((1.0, 2.0)),
    )
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
            1.0,
            1.0,
            10.0,
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
            1.0,
            1.0,
            10.0,
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
    assert (
        "--write not performed: report confirmation was not provided."
        in capsys.readouterr().out
    )
    assert broker.shutdown_called


def test_write_includes_pass_and_warn_but_excludes_fail_and_disabled(
    monkeypatch,
    capsys,
) -> None:
    broker = _prepare_validation_cli(
        monkeypatch,
        ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"),
    )
    schedules = {
        "EURUSD": ("sun 22:01-24:00", "mon 00:00-21:59"),
        "GBPUSD": ("sun 22:01-24:00", "mon 00:00-21:59"),
        "USDJPY": ("sun 22:01-24:00", "mon 00:00-21:59"),
    }

    def validate_one(broker_arg, symbol, symbol_settings, **kwargs):
        assert broker_arg is broker
        if symbol == "EURUSD":
            return SymbolValidationReport(
                symbol, True, True, 4_000, 1, 1, 1, 10,
                schedules[symbol], "US winter", (),
            )
        if symbol == "GBPUSD":
            return SymbolValidationReport(
                symbol, True, True, 4_000, 1, 1, 1, 10,
                schedules[symbol], "US winter", (),
                data_gaps=(datetime(2026, 6, 29, 6, tzinfo=UTC),),
            )
        if symbol == "USDJPY":
            return SymbolValidationReport(
                symbol, True, False, 4_000, 1, 1, 1, 10,
                schedules[symbol], "US winter", ("invalid tick",),
            )
        return SymbolValidationReport(
            symbol, False, False, 4_000, 1, 1, 1, 100,
            (), None, ("disabled-symbol diagnostic",),
        )

    monkeypatch.setattr(validate_symbols_tool, "validate_symbol", validate_one)
    writes: list[tuple[object, ...]] = []
    written_lines = {
        "EURUSD": ('hours_utc = ["sun 22:01-24:00"]', 'hours_utc_season = "US winter"'),
        "GBPUSD": ('hours_utc = ["sun 22:01-24:00"]', 'hours_utc_season = "US winter"'),
    }

    def record_write(*args):
        writes.append(args)
        return written_lines

    monkeypatch.setattr(validate_symbols_tool, "write_hours_utc", record_write)
    monkeypatch.setattr("builtins.input", lambda prompt: "APPLY")

    assert validate_symbols_tool.main(["--write"]) == 1

    assert len(writes) == 1
    assert writes[0][1] == {
        "EURUSD": schedules["EURUSD"],
        "GBPUSD": schedules["GBPUSD"],
    }
    output = capsys.readouterr().out
    assert "EURUSD  yes      PASS" in output
    assert "GBPUSD  yes      WARN" in output
    assert "USDJPY  yes      FAIL" in output
    assert "XAUUSD  no       DISABLED" in output
    assert "GBPUSD: 2026-06-29 06:00 UTC" in output
    assert "--write wrote symbols EURUSD, GBPUSD to " in output
    assert 'hours_utc = ["sun 22:01-24:00"]' in output
    assert 'hours_utc_season = "US winter"' in output
    assert "disabled-symbol diagnostic" not in output.split("FAILURES", maxsplit=1)[1]
    assert broker.shutdown_called


def test_write_is_refused_for_non_demo_account(monkeypatch, capsys) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("EURUSD",), is_demo=False)
    writes: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        validate_symbols_tool,
        "write_hours_utc",
        lambda *args: writes.append(args),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("must not request write confirmation"),
    )

    assert validate_symbols_tool.main(["--write"]) == 1

    output = capsys.readouterr().out
    assert (
        "--write not performed: the connected account is not a demo account."
        in output
    )
    assert writes == []
    assert "FAILURES" in output
    assert broker.shutdown_called


def _freeze_validator_clock(monkeypatch, at_utc: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at_utc.astimezone(tz) if tz is not None else at_utc.replace(tzinfo=None)

    monkeypatch.setattr(validate_symbols_tool, "datetime", FrozenDateTime)


def test_write_reports_no_eligible_symbols(monkeypatch, capsys) -> None:
    _prepare_validation_cli(monkeypatch, ("EURUSD", "XAUUSD"))

    def report_symbol(_broker, symbol, symbol_settings, **kwargs):
        if symbol_settings.enabled:
            return SymbolValidationReport(
                symbol, True, False, 4_000, 1, 1, 1, 10,
                (), None, ("live validation failed",),
            )
        return SymbolValidationReport(
            symbol, False, False, 4_000, 1, 1, 1, 100,
            (), None, ("disabled",),
        )

    monkeypatch.setattr(validate_symbols_tool, "validate_symbol", report_symbol)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("must not request confirmation without schedules"),
    )

    assert validate_symbols_tool.main(["--write"]) == 1

    output = capsys.readouterr().out
    assert (
        "--write not performed: no enabled PASS or WARN symbol has a schedule "
        "to write (FAIL symbols: EURUSD; DISABLED symbols excluded: XAUUSD)."
    ) in output


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (RuntimeError("EURUSD time_msc did not advance"), "no fresh tick"),
        (RuntimeError("EURUSD sanity tick is stale"), "tick too stale"),
        (RuntimeError("MT5 server clock reports UTC+3, expects UTC+2"), "offset mismatch"),
    ],
)
def test_offset_failure_reason_is_actionable(exception, expected) -> None:
    assert validate_symbols_tool._offset_failure_reason(exception) == expected


@pytest.mark.parametrize(
    ("at_utc", "period", "exit_code", "message"),
    [
        (
            datetime(2026, 11, 10, 22, 5, tzinfo=UTC),
            "rollover",
            validate_symbols_tool.EXIT_ROLLOVER_WINDOW,
            "rollover window: spread and offset not evaluated, rerun later",
        ),
        (
            datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
            "weekend",
            validate_symbols_tool.EXIT_WEEKEND_CLOSED,
            "weekend: spread and offset not evaluated, rerun when market opens",
        ),
        (
            datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
            "market_closed",
            validate_symbols_tool.EXIT_MARKET_CLOSED,
            "market closed: spread and offset not evaluated, rerun when market opens",
        ),
    ],
)
def test_write_skips_market_checks_during_rollover_and_weekend(
    monkeypatch,
    capsys,
    at_utc,
    period,
    exit_code,
    message,
) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("EURUSD",))
    settings, _ = load_settings(Path(__file__).parents[1] / "settings.toml")
    if period == "market_closed":
        settings = settings.model_copy(
            update={
                "account": settings.account.model_copy(
                    update={"expected_server": "Demo"}
                ),
                "symbols": {"EURUSD": settings.symbols["EURUSD"]},
                "broker_session_hours_server_time": BrokerSessionHoursServerTime(
                    mon=(),
                    tue=(),
                    wed=(),
                    thu=(),
                    fri=(),
                    sat=(),
                    sun=(),
                )
            }
        )
        monkeypatch.setattr(
            validate_symbols_tool,
            "load_settings",
            lambda _path: (settings, "settings-hash"),
        )
    _freeze_validator_clock(monkeypatch, at_utc)
    monkeypatch.setattr(
        broker,
        "server_utc_offset_hours",
        lambda: pytest.fail("offset must not be evaluated while market is closed"),
    )
    monkeypatch.setattr(
        validate_symbols_tool,
        "sample_live_spread",
        lambda *args: pytest.fail("spread must not be sampled while market is closed"),
    )
    monkeypatch.setattr(
        validate_symbols_tool,
        "validate_symbol",
        lambda *args, **kwargs: pytest.fail("symbol checks must be skipped"),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("must not request confirmation while market is closed"),
    )

    assert validate_symbols_tool.main(["--write"]) == exit_code

    output = capsys.readouterr().out
    assert message in output
    assert f"--write not performed: {message}." in output
    assert "FAILURES\nNone" in output
    assert period == validate_symbols_tool._validation_period(settings, at_utc)


def test_market_closed_period_has_distinct_status(monkeypatch) -> None:
    settings, _ = load_settings(Path(__file__).parents[1] / "settings.toml")
    closed_hours = BrokerSessionHoursServerTime(
        mon=(),
        tue=(),
        wed=(),
        thu=(),
        fri=(),
        sat=(),
        sun=(),
    )
    settings = settings.model_copy(
        update={"broker_session_hours_server_time": closed_hours}
    )

    assert (
        validate_symbols_tool._validation_period(
            settings,
            datetime(2026, 7, 7, 12, tzinfo=UTC),
        )
        == "market_closed"
    )


def test_disabled_symbol_data_is_reported_but_not_added_to_failures(
    monkeypatch,
    capsys,
) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("XAUUSD",))

    def failed_disabled_symbol(broker_arg, symbol, symbol_settings, **kwargs):
        assert broker_arg is broker
        assert not symbol_settings.enabled
        assert "server_session_hours" in kwargs
        return SymbolValidationReport(
            symbol,
            False,
            False,
            4_000,
            1.0,
            1.0,
            2.0,
            100.0,
            (),
            None,
            ("informational diagnostic",),
        )

    monkeypatch.setattr(
        validate_symbols_tool,
        "validate_symbol",
        failed_disabled_symbol,
    )

    assert validate_symbols_tool.main([]) == 0

    output = capsys.readouterr().out
    assert "XAUUSD" in output
    assert "informational diagnostic" not in output.split("FAILURES", maxsplit=1)[1]
    assert output.rstrip().endswith("FAILURES\nNone")
    assert broker.shutdown_called


def test_cli_lists_history_gaps_separately_from_live_failures(
    monkeypatch,
    capsys,
) -> None:
    broker = _prepare_validation_cli(monkeypatch, ("EURUSD",))

    def report_with_gap(broker_arg, symbol, symbol_settings, **kwargs):
        assert broker_arg is broker
        return SymbolValidationReport(
            symbol,
            True,
            False,
            4_000,
            1.0,
            1.2,
            1.0,
            10.0,
            (),
            None,
            ("invalid tick: ask is below bid",),
            data_gaps=(datetime(2026, 6, 29, 6, tzinfo=UTC),),
        )

    monkeypatch.setattr(validate_symbols_tool, "validate_symbol", report_with_gap)

    assert validate_symbols_tool.main([]) == 1

    output = capsys.readouterr().out
    data_gap_section = output.split("DATA GAPS", maxsplit=1)[1].split(
        "FAILURES",
        maxsplit=1,
    )[0]
    failure_section = output.split("FAILURES", maxsplit=1)[1]
    assert "EURUSD: 2026-06-29 06:00 UTC" in data_gap_section
    assert "invalid tick: ask is below bid" in failure_section
    assert "2026-06-29 06:00" not in failure_section
    assert broker.shutdown_called


def test_report_tick_diagnostic_and_datetime_formatting_include_spaces(capsys) -> None:
    report = SymbolValidationReport(
        symbol="EURUSD",
        enabled=True,
        passed=True,
        history_bars=1,
        spread_median_points=1.0,
        spread_p95_points=2.0,
        current_spread_points=3.0,
        spread_limit_points=10.0,
        suggested_hours_utc=("sun 22:01-24:00",),
        suggested_hours_utc_season="US winter",
        issues=(),
        weekday_candle_times=(),
        last_closed_candle_age_hours=1.0,
        tick_freshness=TickFreshnessDiagnostic(
            raw_server_epoch=1_783_029_600,
            converted_time_utc=datetime(2026, 7, 1, 12, tzinfo=UTC),
            current_time_utc=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
            age_seconds=0,
            threshold_seconds=600,
            market_open=True,
            bid=1.1,
            ask=1.10005,
            invalid_reason=None,
            clock_skew_seconds=4.3,
        ),
        data_gaps=(datetime(2026, 6, 29, 6, tzinfo=UTC),),
        live_spread_median_points=1.5,
        live_spread_max_points=3.0,
        live_spread_sample_count=13,
    )

    second_report = replace(report, symbol="GBPUSD", data_gaps=())
    validate_symbols_tool._print_reports([report, second_report])
    output = capsys.readouterr().out

    assert "2026-07-01 12:00" in output
    assert "(reviewed in US winter)" in output
    assert "RECORDED IN CANDLES MEDIAN/P95" in output
    assert "LIVE MEDIAN/MAX" in output
    assert "LAST CLOSED AGE  UTC SESSIONS" in output
    assert "raw tick time (server clock):" in output
    assert output.count("tick diagnostic:") == 2
    assert "tick time converted to UTC: 2026-07-01 12:00" in output
    assert "age: 0.0 s; threshold: 600 s" in output
    assert "bid: 1.10000000; ask: 1.10005000" in output
    assert "tick clock skew: 4.3 s (tick ahead)" in output
    assert "DATA GAP: 2026-06-29 06:00 UTC" in output


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

    written = write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US summer")
    assert written == {
        SYMBOL: (
            (
                'hours_utc = ["sun 21:01-24:00", "mon 00:00-20:59", '
                '"mon 21:01-24:00", "tue 00:00-20:59", "tue 21:01-24:00", '
                '"wed 00:00-20:59", "wed 21:01-24:00", "thu 00:00-20:59", '
                '"thu 21:01-24:00", "fri 00:00-20:57"]'
            ),
            'hours_utc_season = "US summer"',
        )
    }

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

    rewritten_lines = write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US winter")
    assert rewritten_lines == {
        SYMBOL: ('hours_utc_season = "US winter"',)
    }
    assert write_hours_utc(settings_path, {SYMBOL: SESSIONS}, "US winter") == {}
    rewritten = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    assert rewritten["symbols"][SYMBOL]["hours_utc_season"] == "US winter"
