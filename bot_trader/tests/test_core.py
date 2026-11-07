from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bot.app.config import load_settings
from bot.core.indicators import ema
from bot.core.replay import ReplaySettings, replay_symbol
from bot.core.risk import (
    RiskLimits,
    evaluate_entry,
    evaluate_loss_limits,
)
from bot.core.sizing import size_trade
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_a import propose as propose_a
from bot.core.strategy_b import propose as propose_b
from bot.core.timeutil import broker_to_utc, check_broker_offset
from bot.core.types import (
    Candle,
    OpenRisk,
    Proposal,
    ReasonCode,
    Side,
    StrategyResult,
    SymbolSpec,
)

EURUSD = SymbolSpec(
    name="EURUSD",
    base_currency="EUR",
    quote_currency="USD",
    point=0.00001,
    tick_size=0.0001,
    tick_value_loss=10.0,
    volume_min=0.01,
    volume_step=0.01,
    volume_max=100.0,
)
STRATEGY_A_PARAMS = StrategyAParams()


def make_candles(count: int) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            time=start + timedelta(hours=index),
            open=1.1,
            high=1.101,
            low=1.099,
            close=1.1,
        )
        for index in range(count)
    ]


def test_candle_rejects_non_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="UTC"):
        Candle(
            time=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
            open=1.1,
            high=1.101,
            low=1.099,
            close=1.1,
        )


def trending_fixture(side: Side, count: int = 650) -> list[Candle]:
    direction = 1.0 if side is Side.LONG else -1.0
    closes = [1.0 + direction * index * 0.0001 for index in range(count)]
    ema20 = ema(closes, 20)
    candles = [
        Candle(
            time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index),
            open=close,
            high=close + 0.0003,
            low=close - 0.0003,
            close=close,
        )
        for index, close in enumerate(closes)
    ]
    touched_index = count - 5
    touched = candles[touched_index]
    candles[touched_index] = Candle(
        time=touched.time,
        open=touched.open,
        high=max(touched.high, ema20[touched_index]) if side is Side.SHORT else touched.high,
        low=min(touched.low, ema20[touched_index]) if side is Side.LONG else touched.low,
        close=touched.close,
    )
    previous_index = count - 2
    previous = candles[previous_index]
    candles[previous_index] = Candle(
        time=previous.time,
        open=previous.open,
        high=closes[previous_index] + 0.00005 if side is Side.LONG else previous.high,
        low=previous.low if side is Side.LONG else closes[previous_index] - 0.00005,
        close=previous.close,
    )
    return candles


def test_config_template_validates_without_terminal_connection() -> None:
    path = Path(__file__).parents[1] / "settings.toml"
    settings, version = load_settings(path)
    assert settings.account.mode == "demo"
    assert settings.account.allow_live is False
    assert settings.symbols["XAUUSD"].enabled is False
    strategy_params = settings.strategy_a.to_params()
    assert strategy_params.min_candles == 600
    assert strategy_params.window_candles == 1_000
    assert len(version) == 64


def test_config_refuses_live_mode_and_strategy_parameter_changes(tmp_path) -> None:
    source = Path(__file__).parents[1] / "settings.toml"
    text = source.read_text(encoding="utf-8")
    live_settings = tmp_path / "live.toml"
    live_settings.write_text(text.replace('mode = "demo"', 'mode = "live"'), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(live_settings)

    changed_strategy = tmp_path / "changed-strategy.toml"
    changed_strategy.write_text(text.replace("ema_fast = 20", "ema_fast = 21"), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed to the approved design"):
        load_settings(changed_strategy)


def test_core_dependency_boundary_is_preserved() -> None:
    core_dir = Path(__file__).parents[1] / "bot" / "core"
    forbidden = ("MetaTrader5", "sqlite3", "requests", "datetime.now", "time.time")
    for source_path in core_dir.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        forbidden_for_module = forbidden
        if source_path.name == "clock.py":
            forbidden_for_module = forbidden[:3]
        assert not any(token in source for token in forbidden_for_module), source_path.name


def test_strategies_return_warmup_for_short_history() -> None:
    candles = make_candles(100)
    assert propose_a(candles, STRATEGY_A_PARAMS).reason is ReasonCode.WARMUP
    assert propose_b(candles).reason is ReasonCode.WARMUP


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_strategy_a_warmup_boundary_is_600_closed_candles(side: Side) -> None:
    assert propose_a(trending_fixture(side, 599), STRATEGY_A_PARAMS).reason is ReasonCode.WARMUP
    assert propose_a(trending_fixture(side, 600), STRATEGY_A_PARAMS).proposal is not None


def test_strategy_a_uses_exactly_its_last_1000_candles() -> None:
    candles = trending_fixture(Side.LONG, 1_100)

    assert propose_a(candles, STRATEGY_A_PARAMS) == propose_a(
        candles[-1_000:], STRATEGY_A_PARAMS
    )


def test_strategy_a_uses_explicit_warmup_and_target_parameters() -> None:
    candles = trending_fixture(Side.LONG, 600)
    params = replace(
        STRATEGY_A_PARAMS,
        min_candles=601,
        take_profit_r=2.4,
    )
    assert propose_a(candles, params).reason is ReasonCode.WARMUP

    ready_params = replace(params, min_candles=600)
    result = propose_a(candles, ready_params)

    assert result.proposal is not None
    risk_distance = result.proposal.entry - result.proposal.stop_loss
    assert result.proposal.take_profit - result.proposal.entry == pytest.approx(
        2.4 * risk_distance
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_strategy_a_returns_proposal_for_fixed_trend_pullback(side: Side) -> None:
    result = propose_a(trending_fixture(side), STRATEGY_A_PARAMS)
    assert result.proposal is not None
    assert result.proposal.side is side
    assert result.proposal.reason == "STRATEGY_A"
    assert abs(result.proposal.entry - result.proposal.stop_loss) > 0


def test_strategy_b_rejects_non_h1_timeframe() -> None:
    with pytest.raises(ValueError, match="H1"):
        propose_b(make_candles(300), timeframe="M15")


@given(
    equity=st.floats(min_value=200.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
    risk_pct=st.floats(min_value=0.05, max_value=0.5, allow_nan=False, allow_infinity=False),
)
def test_position_sizing_never_exceeds_risk_or_volume_step(equity: float, risk_pct: float) -> None:
    proposal = Proposal(Side.LONG, 1.1, 1.099, 1.102, "TEST")
    sized = size_trade(
        equity,
        risk_pct,
        proposal,
        EURUSD,
        bid_price=proposal.entry,
        ask_price=proposal.entry + 0.00001,
        commission_per_lot_side=3.5,
        slippage_price=0.00001,
    )
    if sized.lots is None:
        assert sized.reason == "BELOW_MIN_LOT"
        return
    assert sized.actual_risk is not None
    assert sized.actual_risk <= equity * risk_pct / 100.0
    assert sized.lots / EURUSD.volume_step == pytest.approx(round(sized.lots / EURUSD.volume_step))


def test_cost_gate_rejects_expensive_spread() -> None:
    proposal = Proposal(Side.LONG, 1.1, 1.099, 1.102, "TEST")
    sized = size_trade(
        3_000.0,
        0.5,
        proposal,
        EURUSD,
        bid_price=proposal.entry,
        ask_price=proposal.entry + 0.001,
        commission_per_lot_side=3.5,
        slippage_price=0.00001,
    )
    assert sized.reason == "COST_TOO_HIGH"
    assert sized.lots is None


def test_daily_headroom_does_not_credit_closed_profit() -> None:
    positions = [OpenRisk("EURUSD", Side.LONG, 0.5)]
    result = evaluate_entry(
        symbol="GBPUSD",
        side=Side.LONG,
        new_risk_pct=0.5,
        positions=positions,
        pending=[],
        specs={
            "EURUSD": EURUSD,
            "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        },
        realized_pnl_today=15.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
    )
    assert result.allowed
    assert result.daily_headroom_pct == pytest.approx(0.5)


def test_realized_profit_does_not_raise_daily_headroom() -> None:
    result = evaluate_entry(
        symbol="EURUSD",
        side=Side.LONG,
        new_risk_pct=0.51,
        positions=[],
        pending=[],
        specs={"EURUSD": EURUSD},
        realized_pnl_today=300.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
        limits=RiskLimits(daily_loss_pct=0.5),
    )

    assert not result.allowed
    assert result.reason == "DAILY_HEADROOM"
    assert result.daily_headroom_pct == pytest.approx(0.5)


def test_daily_headroom_subtracts_full_risk_of_open_trades() -> None:
    positions = [
        OpenRisk("EURUSD", Side.LONG, 0.5),
        OpenRisk("GBPUSD", Side.SHORT, 0.5),
    ]
    result = evaluate_entry(
        symbol="AUDUSD",
        side=Side.LONG,
        new_risk_pct=0.1,
        positions=positions,
        pending=[],
        specs={
            "EURUSD": EURUSD,
            "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
            "AUDUSD": SymbolSpec("AUDUSD", "AUD", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        },
        realized_pnl_today=0.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
    )

    assert not result.allowed
    assert result.reason == "DAILY_HEADROOM"
    assert result.daily_headroom_pct == 0.0


def test_net_currency_exposure_refuses_excess_dollar_risk() -> None:
    positions = [
        OpenRisk("EURUSD", Side.LONG, 0.2),
        OpenRisk("GBPUSD", Side.LONG, 0.2),
    ]
    specs = {
        "EURUSD": EURUSD,
        "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "AUDUSD": SymbolSpec("AUDUSD", "AUD", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
    }
    result = evaluate_entry(
        symbol="AUDUSD",
        side=Side.LONG,
        new_risk_pct=0.2,
        positions=positions,
        pending=[],
        specs=specs,
        realized_pnl_today=30.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
        limits=RiskLimits(max_net_currency_exposure_pct=0.5),
    )
    assert not result.allowed
    assert result.reason == "CURRENCY_EXPOSURE"


@pytest.mark.parametrize(
    ("positions", "symbol", "reason"),
    [
        ([OpenRisk("EURUSD", Side.LONG, 0.2)], "EURUSD", "SLOT_TAKEN"),
        (
            [
                OpenRisk("EURUSD", Side.LONG, 0.1),
                OpenRisk("GBPUSD", Side.SHORT, 0.1),
                OpenRisk("AUDUSD", Side.LONG, 0.1),
            ],
            "XAUUSD",
            "MAX_TRADES",
        ),
    ],
)
def test_entry_enforces_symbol_slot_and_trade_count(
    positions: list[OpenRisk], symbol: str, reason: str
) -> None:
    specs = {
        "EURUSD": EURUSD,
        "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "AUDUSD": SymbolSpec("AUDUSD", "AUD", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "XAUUSD": SymbolSpec("XAUUSD", "XAU", "USD", 0.01, 0.01, 1, 0.01, 0.01, 100),
    }

    result = evaluate_entry(
        symbol=symbol,
        side=Side.LONG,
        new_risk_pct=0.1,
        positions=positions,
        pending=[],
        specs=specs,
        realized_pnl_today=30.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
    )

    assert not result.allowed
    assert result.reason == reason


@pytest.mark.parametrize(
    ("positions", "symbol", "new_risk_pct", "reason"),
    [
        (
            [
                OpenRisk("EURUSD", Side.LONG, 0.3),
                OpenRisk("GBPUSD", Side.SHORT, 0.3),
                OpenRisk("AUDUSD", Side.LONG, 0.3),
            ],
            "XAUUSD",
            0.2,
            "MAX_TRADES",
        ),
        (
            [
                OpenRisk("EURUSD", Side.LONG, 0.4),
                OpenRisk("GBPUSD", Side.LONG, 0.4),
            ],
            "AUDUSD",
            0.3,
            "CURRENCY_EXPOSURE",
        ),
    ],
)
def test_entry_risk_reason_precedence(
    positions: list[OpenRisk], symbol: str, new_risk_pct: float, reason: str
) -> None:
    specs = {
        "EURUSD": EURUSD,
        "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "AUDUSD": SymbolSpec("AUDUSD", "AUD", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "XAUUSD": SymbolSpec("XAUUSD", "XAU", "USD", 0.01, 0.01, 1, 0.01, 0.01, 100),
    }

    result = evaluate_entry(
        symbol=symbol,
        side=Side.LONG,
        new_risk_pct=new_risk_pct,
        positions=positions,
        pending=[],
        specs=specs,
        realized_pnl_today=0.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
    )

    assert not result.allowed
    assert result.reason == reason


@pytest.mark.parametrize(
    ("current_equity", "halted_daily", "halted_monthly"),
    [(9_900.0, True, False), (9_700.0, True, True)],
)
def test_equity_loss_limits_include_floating_loss(
    current_equity: float, halted_daily: bool, halted_monthly: bool
) -> None:
    status = evaluate_loss_limits(
        start_of_day_equity=10_000.0,
        start_of_month_equity=10_000.0,
        current_equity=current_equity,
    )

    assert status.halted_daily is halted_daily
    assert status.halted_monthly is halted_monthly


def test_risk_rejects_non_finite_inputs() -> None:
    with pytest.raises(ValueError, match="finite"):
        RiskLimits(daily_loss_pct=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        OpenRisk("EURUSD", Side.LONG, float("nan"))
    with pytest.raises(ValueError, match="finite"):
        evaluate_entry(
            symbol="EURUSD",
            side=Side.LONG,
            new_risk_pct=0.5,
            positions=[],
            pending=[],
            specs={"EURUSD": EURUSD},
            realized_pnl_today=0.0,
            start_of_day_equity=3_000.0,
            margin_used_after_pct=float("nan"),
            bars_since_stop=None,
        )


@pytest.mark.parametrize(
    ("bars_since_stop", "allowed", "reason"),
    [(5, False, "COOLDOWN"), (6, True, None)],
)
def test_six_candle_stop_cooldown(
    bars_since_stop: int, allowed: bool, reason: str | None
) -> None:
    result = evaluate_entry(
        symbol="EURUSD",
        side=Side.LONG,
        new_risk_pct=0.5,
        positions=[],
        pending=[],
        specs={"EURUSD": EURUSD},
        realized_pnl_today=0.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=bars_since_stop,
    )

    assert result.allowed is allowed
    assert result.reason == reason


@pytest.mark.parametrize(
    ("margin_used_after_pct", "allowed", "reason"),
    [(30.0, True, None), (30.01, False, "NO_MARGIN")],
)
def test_margin_limit_boundary(
    margin_used_after_pct: float, allowed: bool, reason: str | None
) -> None:
    result = evaluate_entry(
        symbol="EURUSD",
        side=Side.LONG,
        new_risk_pct=0.5,
        positions=[],
        pending=[],
        specs={"EURUSD": EURUSD},
        realized_pnl_today=0.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=margin_used_after_pct,
        bars_since_stop=None,
    )

    assert result.allowed is allowed
    assert result.reason == reason


@given(
    position_risks=st.lists(
        st.floats(min_value=0.01, max_value=0.5, allow_nan=False),
        min_size=0,
        max_size=3,
    ),
    new_risk=st.floats(min_value=0.01, max_value=0.5, allow_nan=False),
)
def test_accepted_entry_respects_currency_exposure_caps(
    position_risks: list[float], new_risk: float
) -> None:
    specs = {
        "EURUSD": EURUSD,
        "GBPUSD": SymbolSpec("GBPUSD", "GBP", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "AUDUSD": SymbolSpec("AUDUSD", "AUD", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100),
        "XAUUSD": SymbolSpec("XAUUSD", "XAU", "USD", 0.01, 0.01, 1, 0.01, 0.01, 100),
    }
    symbols = ("EURUSD", "GBPUSD", "AUDUSD")
    positions = [
        OpenRisk(symbol, Side.LONG, risk_pct)
        for symbol, risk_pct in zip(symbols, position_risks)
    ]
    result = evaluate_entry(
        symbol="XAUUSD",
        side=Side.LONG,
        new_risk_pct=new_risk,
        positions=positions,
        pending=[],
        specs=specs,
        realized_pnl_today=3_000.0,
        start_of_day_equity=3_000.0,
        margin_used_after_pct=0.0,
        bars_since_stop=None,
    )

    if result.allowed:
        assert len(positions) + 1 <= RiskLimits().max_open_trades
        assert all(abs(exposure) <= 1.0 + 1e-12 for _, exposure in result.currency_exposure_pct)


@pytest.mark.parametrize(
    ("utc_time", "server_offset_hours"),
    [
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 3),
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 2),
        (datetime(2027, 3, 14, 12, tzinfo=UTC), 3),
    ],
)
def test_broker_time_conversion_across_us_dst(utc_time: datetime, server_offset_hours: int) -> None:
    server_epoch = int((utc_time + timedelta(hours=server_offset_hours)).timestamp())
    assert broker_to_utc(server_epoch, server_offset_hours) == utc_time


def test_broker_offset_startup_check_warns_when_mismatch() -> None:
    utc_time = datetime(2026, 11, 1, 12, tzinfo=UTC)
    with pytest.warns(RuntimeWarning, match="Broker offset 1 does not match the expected New York offset"):
        check_broker_offset(1, utc_time=utc_time)


def test_replay_fills_next_open_and_assumes_stop_first_when_both_hit() -> None:
    candles = make_candles(302)
    signal_time = candles[300].time
    candles[301] = Candle(
        time=candles[301].time,
        open=1.1,
        high=1.13,
        low=1.08,
        close=1.11,
        spread=0.00001,
    )

    def strategy(history: list[Candle]) -> StrategyResult:
        if history[-1].time == signal_time:
            return StrategyResult(
                Proposal(Side.LONG, 1.1, 1.09, 1.12, "TEST"),
                None,
            )
        return StrategyResult(None, ReasonCode.NO_TRIGGER)

    result = replay_symbol(
        candles,
        EURUSD,
        strategy,
        ReplaySettings(
            starting_equity=3_000,
            commission_per_lot_side=3.5,
            slippage_price=0.00001,
            typical_spread_price=0.00001,
        ),
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == candles[301].time
    assert trade.entry_price == pytest.approx(1.10002)
    assert trade.exit_reason == "STOP"
    assert trade.exit_price < trade.entry_price
    assert trade.pnl < 0


def test_replay_strategy_only_receives_closed_candles() -> None:
    candles = make_candles(5)
    observed_histories: list[tuple[datetime, ...]] = []

    def strategy(history: list[Candle]) -> StrategyResult:
        observed_histories.append(tuple(candle.time for candle in history))
        return StrategyResult(None, ReasonCode.NO_TRIGGER)

    replay_symbol(candles, EURUSD, strategy)

    assert observed_histories == [
        tuple(candle.time for candle in candles[:index])
        for index in range(1, len(candles))
    ]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_replay_strategy_a_signal_is_unchanged_by_later_candles(side: Side) -> None:
    signal_history = trending_fixture(side)
    next_candle = Candle(
        time=signal_history[-1].time + timedelta(hours=1),
        open=signal_history[-1].close,
        high=signal_history[-1].close + 0.0001,
        low=signal_history[-1].close - 0.0001,
        close=signal_history[-1].close,
    )
    later_candle = replace(
        next_candle,
        time=next_candle.time + timedelta(hours=1),
    )

    def signal_for(data: list[Candle]) -> StrategyResult:
        results: list[StrategyResult] = []

        def strategy(history: list[Candle]) -> StrategyResult:
            result = propose_a(history, STRATEGY_A_PARAMS)
            if history[-1].time == signal_history[-1].time:
                results.append(result)
            return result

        replay_symbol(data, EURUSD, strategy)
        assert len(results) == 1
        return results[0]

    with_one_future_bar = signal_for([*signal_history, next_candle])
    with_additional_future_bar = signal_for(
        [*signal_history, next_candle, later_candle]
    )

    assert with_one_future_bar == with_additional_future_bar
    assert with_one_future_bar.proposal is not None
    assert with_one_future_bar.proposal.side is side


def test_replay_rejects_duplicate_or_out_of_order_bars() -> None:
    candles = make_candles(3)
    candles[2] = Candle(
        time=candles[1].time,
        open=1.1,
        high=1.101,
        low=1.099,
        close=1.1,
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        replay_symbol(candles, EURUSD, lambda _: StrategyResult(None, None))


def test_replay_applies_strategy_b_time_stop_after_exact_candle_count() -> None:
    candles = make_candles(5)
    signal_time = candles[1].time

    def strategy(history: list[Candle]) -> StrategyResult:
        if history[-1].time == signal_time:
            return StrategyResult(
                Proposal(Side.LONG, 1.1, 1.09, 1.12, "STRATEGY_B"),
                None,
            )
        return StrategyResult(None, ReasonCode.NO_TRIGGER)

    result = replay_symbol(
        candles,
        EURUSD,
        strategy,
        ReplaySettings(
            starting_equity=3_000,
            commission_per_lot_side=3.5,
            time_stop_candles=2,
        ),
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "TIME_STOP"
    assert result.trades[0].exit_time == candles[3].time
