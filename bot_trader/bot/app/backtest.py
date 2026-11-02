from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bot.adapters.broker import (
    BrokerAccount,
    BrokerPosition,
    BrokerResult,
    OrderRequest,
    Tick,
)
from bot.adapters.sim_broker import SimBroker, SimCosts
from bot.app.data import DataQualityError, Timeframe, last_closed_candles, validate_candles
from bot.core.clock import SimClock
from bot.core.replay import ReplayPosition, ReplaySettings, ReplayTrade
from bot.core.risk import RiskLimits, evaluate_entry, evaluate_loss_limits
from bot.core.sizing import size_trade
from bot.core.strategy_a import StrategyAParams
from bot.core.strategy_a import propose as propose_strategy_a
from bot.core.timeutil import (
    expected_new_york_offset,
    month_start_utc,
    trading_day_start_utc,
)
from bot.core.types import Candle, Proposal, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class EquityPoint:
    time_utc: datetime
    equity: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    timeframe: Timeframe
    starting_equity: float
    ending_equity: float
    trades: tuple[ReplayTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_trade: ReplayPosition | None
    skipped_cost: int
    skipped_size: int
    skipped_risk: int
    excluded_recent_bars: int
    history_gap_count: int


@dataclass(frozen=True, slots=True)
class _ActiveTrade:
    ticket: str
    proposal: Proposal
    lots: float
    actual_risk: float
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    entry_commission: float
    entry_index: int


def select_backtest_history(
    candles: tuple[Candle, ...],
    timeframe: Timeframe,
    *,
    now_utc: datetime,
    final: bool = False,
) -> tuple[tuple[Candle, ...], int]:
    """Keep recent out-of-sample bars out unless explicitly running --final."""
    closed = last_closed_candles(candles, timeframe, now_utc=now_utc)
    if final:
        return closed, 0
    cutoff = _one_year_before(_require_utc(now_utc, "now_utc"))
    selected = tuple(candle for candle in closed if candle.time < cutoff)
    excluded = len(closed) - len(selected)
    if not selected:
        raise DataQualityError(
            "No eligible backtest candles remain after excluding the most recent 12 months; "
            "use --final only for the final evaluation run."
        )
    validate_candles(selected, timeframe)
    return selected, excluded


def run_strategy_a_backtest(
    candles: tuple[Candle, ...],
    spec: SymbolSpec,
    params: StrategyAParams,
    *,
    timeframe: Timeframe,
    now_utc: datetime,
    magic_number: int,
    settings: ReplaySettings | None = None,
    risk_limits: RiskLimits | None = None,
    costs: SimCosts | None = None,
    final: bool = False,
    history_gap_count: int = 0,
) -> BacktestResult:
    """Run closed-bar Strategy A through SimBroker with risk and costs enabled."""
    settings = settings or ReplaySettings()
    risk_limits = risk_limits or RiskLimits()
    costs = costs or SimCosts(
        commission_per_lot_side=settings.commission_per_lot_side,
        slippage_points=settings.slippage_price / spec.point,
    )
    history, excluded_recent = select_backtest_history(
        candles,
        timeframe,
        now_utc=now_utc,
        final=final,
    )
    if magic_number <= 0:
        raise ValueError("magic_number must be positive")
    if history_gap_count < 0:
        raise ValueError("history_gap_count cannot be negative")

    clock = SimClock(history[0].time)
    initial_spread = _spread(history[0], settings.typical_spread_price)
    broker = SimBroker(
        account_snapshot=BrokerAccount(
            login=0,
            server="SimBroker",
            currency="USD",
            balance=settings.starting_equity,
            equity=settings.starting_equity,
            is_demo=True,
            trade_allowed=True,
            margin_mode="hedging",
        ),
        specs={spec.name: spec},
        tick_data={
            spec.name: Tick(
                history[0].time,
                history[0].open,
                history[0].open + initial_spread,
            )
        },
        clock=clock,
        costs={spec.name: costs},
    )
    trades: list[ReplayTrade] = []
    equity_curve = [EquityPoint(history[0].time, settings.starting_equity)]
    active_trade: _ActiveTrade | None = None
    day_key: datetime | None = None
    month_key: datetime | None = None
    day_start_balance = settings.starting_equity
    day_start_equity = settings.starting_equity
    month_start_equity = settings.starting_equity
    last_stop_index: int | None = None
    skipped_cost = 0
    skipped_size = 0
    skipped_risk = 0

    for index in range(1, len(history)):
        bar = history[index]
        spread = _spread(bar, settings.typical_spread_price)
        broker.tick_data[spec.name] = Tick(bar.time, bar.open, bar.open + spread)
        clock.set(bar.time)

        current_account = broker.account()
        offset = expected_new_york_offset(bar.time)
        current_day = trading_day_start_utc(bar.time, offset)
        current_month = month_start_utc(bar.time, offset)
        if current_day != day_key:
            day_key = current_day
            day_start_balance = current_account.balance
            day_start_equity = current_account.equity
        if current_month != month_key:
            month_key = current_month
            month_start_equity = current_account.equity

        positions_before_bar = broker.positions(magic_number)
        if positions_before_bar:
            close_results = broker.process_candle(spec.name, bar)
            for close_result in close_results:
                active_trade = _record_close(
                    close_result,
                    active_trade,
                    bar,
                    spec,
                    trades,
                )
                if active_trade is None and _was_stop(close_result, positions_before_bar):
                    last_stop_index = index
        else:
            current_account = broker.account()
            limits = evaluate_loss_limits(
                start_of_day_equity=day_start_equity,
                start_of_month_equity=month_start_equity,
                current_equity=current_account.equity,
                limits=risk_limits,
            )
            signal_candles = history[max(0, index - params.window_candles) : index]
            result = propose_strategy_a(signal_candles, params)
            proposal = result.proposal
            if proposal is not None and not limits.halted_daily and not limits.halted_monthly:
                sized = size_trade(
                    current_account.equity,
                    settings.risk_per_trade_pct,
                    proposal,
                    spec,
                    bid_price=bar.open,
                    ask_price=bar.open + spread,
                    commission_per_lot_side=costs.commission_per_lot_side,
                    slippage_price=costs.slippage_points * spec.point,
                    cost_gate_pct_of_risk=settings.cost_gate_pct_of_risk,
                )
                if sized.reason == "COST_TOO_HIGH":
                    skipped_cost += 1
                elif sized.lots is None or sized.actual_risk is None:
                    skipped_size += 1
                else:
                    request = OrderRequest(
                        symbol=spec.name,
                        side=proposal.side,
                        lots=sized.lots,
                        stop_loss=proposal.stop_loss,
                        take_profit=proposal.take_profit,
                        magic_number=magic_number,
                        intent_id=f"replay-{index}",
                    )
                    margin = broker.calculate_margin(request)
                    if margin is None or current_account.equity <= 0:
                        skipped_risk += 1
                    else:
                        margin_pct = margin / current_account.equity * 100
                        bars_since_stop = (
                            None
                            if last_stop_index is None
                            else max(0, index - last_stop_index - 1)
                        )
                        risk_result = evaluate_entry(
                            symbol=spec.name,
                            side=proposal.side,
                            new_risk_pct=(
                                sized.actual_risk / current_account.equity * 100
                            ),
                            positions=[],
                            pending=[],
                            specs={spec.name: spec},
                            realized_pnl_today=current_account.balance - day_start_balance,
                            start_of_day_equity=day_start_equity,
                            margin_used_after_pct=margin_pct,
                            bars_since_stop=bars_since_stop,
                            limits=risk_limits,
                        )
                        if risk_result.allowed:
                            check = broker.check(request)
                            if check.code != "DONE":
                                raise RuntimeError(
                                    f"SimBroker order check failed at candle {index}: {check.code}"
                                )
                            send_result = broker.send_market(request)
                            if send_result is None or send_result.code != "DONE":
                                code = "UNKNOWN" if send_result is None else send_result.code
                                raise RuntimeError(
                                    f"SimBroker order send failed at candle {index}: {code}"
                                )
                            if send_result.ticket is None or send_result.price is None:
                                raise RuntimeError("SimBroker accepted an order without a fill")
                            active_trade = _ActiveTrade(
                                ticket=send_result.ticket,
                                proposal=proposal,
                                lots=sized.lots,
                                actual_risk=sized.actual_risk,
                                signal_time=signal_candles[-1].time,
                                entry_time=bar.time,
                                entry_price=send_result.price,
                                entry_commission=send_result.commission or 0.0,
                                entry_index=index,
                            )
                        else:
                            skipped_risk += 1
            elif proposal is not None:
                skipped_risk += 1

            positions_for_bar = broker.positions(magic_number)
            close_results = broker.process_candle(spec.name, bar)
            for close_result in close_results:
                active_trade = _record_close(
                    close_result,
                    active_trade,
                    bar,
                    spec,
                    trades,
                )
                if active_trade is None and _was_stop(close_result, positions_for_bar):
                    last_stop_index = index

        equity_curve.append(EquityPoint(bar.time, broker.account().equity))

    open_position: ReplayPosition | None = None
    if active_trade is not None:
        held = len(history) - 1 - active_trade.entry_index
        open_position = ReplayPosition(
            side=active_trade.proposal.side,
            lots=active_trade.lots,
            signal_time=active_trade.signal_time,
            entry_time=active_trade.entry_time,
            entry_price=active_trade.entry_price,
            stop_loss=active_trade.proposal.stop_loss,
            take_profit=active_trade.proposal.take_profit,
            actual_risk=active_trade.actual_risk,
            candles_held=held,
        )
    account = broker.account()
    return BacktestResult(
        symbol=spec.name,
        timeframe=timeframe,
        starting_equity=settings.starting_equity,
        ending_equity=account.equity,
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        open_trade=open_position,
        skipped_cost=skipped_cost,
        skipped_size=skipped_size,
        skipped_risk=skipped_risk,
        excluded_recent_bars=excluded_recent,
        history_gap_count=history_gap_count,
    )


def _record_close(
    close_result: BrokerResult,
    active: _ActiveTrade | None,
    candle: Candle,
    spec: SymbolSpec,
    trades: list[ReplayTrade],
) -> _ActiveTrade | None:
    if active is None or close_result.ticket != active.ticket:
        raise RuntimeError("SimBroker closed a position that was not tracked by replay")
    if close_result.price is None or close_result.lots is None:
        raise RuntimeError("SimBroker closed a position without price/volume details")
    direction = 1 if active.proposal.side is Side.LONG else -1
    gross = (
        (close_result.price - active.entry_price)
        / spec.tick_size
        * spec.tick_value_loss
        * active.lots
        * direction
    )
    exit_commission = close_result.commission or 0.0
    pnl = gross - active.entry_commission - exit_commission
    exit_reason = (
        "STOP"
        if (
            close_result.price <= active.proposal.stop_loss
            if active.proposal.side is Side.LONG
            else close_result.price >= active.proposal.stop_loss
        )
        else "TARGET"
    )
    trades.append(
        ReplayTrade(
            side=active.proposal.side,
            lots=active.lots,
            signal_time=active.signal_time,
            entry_time=active.entry_time,
            exit_time=candle.time,
            entry_price=active.entry_price,
            exit_price=close_result.price,
            stop_loss=active.proposal.stop_loss,
            take_profit=active.proposal.take_profit,
            actual_risk=active.actual_risk,
            pnl=pnl,
            result_r=pnl / active.actual_risk,
            exit_reason=exit_reason,
        )
    )
    return None


def _was_stop(result: BrokerResult, positions: tuple[BrokerPosition, ...]) -> bool:
    position = next(
        (item for item in positions if item.ticket == result.ticket),
        None,
    )
    if position is None or result.price is None:
        return False
    return (
        result.price <= position.stop_loss
        if position.side is Side.LONG
        else result.price >= position.stop_loss
    )


def _spread(candle: Candle, fallback: float) -> float:
    return candle.spread if candle.spread > 0 else fallback


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _one_year_before(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)
