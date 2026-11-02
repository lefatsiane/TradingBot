from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from math import isfinite

from bot.core.sizing import size_trade
from bot.core.types import Candle, Proposal, Side, StrategyResult, SymbolSpec

Strategy = Callable[[Sequence[Candle]], StrategyResult]


@dataclass(frozen=True, slots=True)
class ReplaySettings:
    starting_equity: float = 3_000.0
    risk_per_trade_pct: float = 0.5
    commission_per_lot_side: float = 3.5
    slippage_price: float = 0.0
    typical_spread_price: float = 0.0
    cost_gate_pct_of_risk: float = 15.0
    time_stop_candles: int | None = None

    def __post_init__(self) -> None:
        numeric_values = (
            self.starting_equity,
            self.risk_per_trade_pct,
            self.commission_per_lot_side,
            self.slippage_price,
            self.typical_spread_price,
            self.cost_gate_pct_of_risk,
        )
        if not all(isfinite(value) for value in numeric_values):
            raise ValueError("Replay values must be finite")
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if not 0 < self.risk_per_trade_pct <= 0.5:
            raise ValueError("risk_per_trade_pct must be in (0, 0.5]")
        if (
            min(
                self.commission_per_lot_side,
                self.slippage_price,
                self.typical_spread_price,
                self.cost_gate_pct_of_risk,
            )
            < 0
        ):
            raise ValueError("Replay costs and cost gate cannot be negative")
        if self.time_stop_candles is not None and self.time_stop_candles < 1:
            raise ValueError("time_stop_candles must be positive")


@dataclass(frozen=True, slots=True)
class ReplayTrade:
    side: Side
    lots: float
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    actual_risk: float
    pnl: float
    result_r: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class ReplayPosition:
    side: Side
    lots: float
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    actual_risk: float
    candles_held: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    starting_equity: float
    ending_equity: float
    trades: tuple[ReplayTrade, ...]
    open_trade: ReplayPosition | None
    skipped_cost: int
    skipped_size: int


@dataclass(slots=True)
class _OpenTrade:
    proposal: Proposal
    lots: float
    actual_risk: float
    signal_index: int
    entry_time: datetime
    entry_price: float
    candles_held: int = 0


def _spread(candle: Candle, typical_spread_price: float) -> float:
    return candle.spread if candle.spread > 0 else typical_spread_price


def _exit_for_bar(
    trade: _OpenTrade,
    candle: Candle,
    spec: SymbolSpec,
    settings: ReplaySettings,
) -> tuple[float, str] | None:
    spread = _spread(candle, settings.typical_spread_price)
    trade.candles_held += 1

    if trade.proposal.side is Side.LONG:
        if candle.open <= trade.proposal.stop_loss:
            exit_price, reason = candle.open, "STOP_GAP"
        elif candle.low <= trade.proposal.stop_loss:
            exit_price, reason = trade.proposal.stop_loss, "STOP"
        elif candle.open >= trade.proposal.take_profit:
            exit_price, reason = candle.open, "TARGET_GAP"
        elif candle.high >= trade.proposal.take_profit:
            exit_price, reason = trade.proposal.take_profit, "TARGET"
        else:
            exit_price = reason = None
    else:
        ask_open = candle.open + spread
        ask_high = candle.high + spread
        ask_low = candle.low + spread
        if ask_open >= trade.proposal.stop_loss:
            exit_price, reason = ask_open, "STOP_GAP"
        elif ask_high >= trade.proposal.stop_loss:
            exit_price, reason = trade.proposal.stop_loss, "STOP"
        elif ask_open <= trade.proposal.take_profit:
            exit_price, reason = ask_open, "TARGET_GAP"
        elif ask_low <= trade.proposal.take_profit:
            exit_price, reason = trade.proposal.take_profit, "TARGET"
        else:
            exit_price = reason = None

    if exit_price is not None:
        adverse_slippage = (
            settings.slippage_price
            if trade.proposal.side is Side.SHORT
            else -settings.slippage_price
        )
        return exit_price + adverse_slippage, str(reason)
    if settings.time_stop_candles is not None and trade.candles_held >= settings.time_stop_candles:
        market_exit = candle.close if trade.proposal.side is Side.LONG else candle.close + spread
        adverse_slippage = (
            settings.slippage_price
            if trade.proposal.side is Side.SHORT
            else -settings.slippage_price
        )
        return market_exit + adverse_slippage, "TIME_STOP"
    return None


def _pnl(
    trade: _OpenTrade,
    exit_price: float,
    spec: SymbolSpec,
    settings: ReplaySettings,
) -> float:
    direction = 1.0 if trade.proposal.side is Side.LONG else -1.0
    gross = (
        (exit_price - trade.entry_price)
        / spec.tick_size
        * spec.tick_value_loss
        * trade.lots
        * direction
    )
    return gross - 2.0 * settings.commission_per_lot_side * trade.lots


def replay_symbol(
    candles: Sequence[Candle],
    spec: SymbolSpec,
    strategy: Strategy,
    settings: ReplaySettings | None = None,
) -> ReplayResult:
    """Replay one symbol; signals use closed bars and fill at the following bar open."""
    if settings is None:
        settings = ReplaySettings()
    if any(right.time <= left.time for left, right in pairwise(candles)):
        raise ValueError("Candle times must be strictly increasing and unique")
    equity = settings.starting_equity
    trades: list[ReplayTrade] = []
    open_trade: _OpenTrade | None = None
    skipped_cost = 0
    skipped_size = 0

    for bar_index in range(1, len(candles)):
        bar = candles[bar_index]
        if open_trade is not None:
            exit_event = _exit_for_bar(open_trade, bar, spec, settings)
            if exit_event is None:
                continue
            exit_price, exit_reason = exit_event
            profit = _pnl(open_trade, exit_price, spec, settings)
            equity += profit
            trades.append(
                ReplayTrade(
                    side=open_trade.proposal.side,
                    lots=open_trade.lots,
                    signal_time=candles[open_trade.signal_index].time,
                    entry_time=open_trade.entry_time,
                    exit_time=bar.time,
                    entry_price=open_trade.entry_price,
                    exit_price=exit_price,
                    stop_loss=open_trade.proposal.stop_loss,
                    take_profit=open_trade.proposal.take_profit,
                    actual_risk=open_trade.actual_risk,
                    pnl=profit,
                    result_r=profit / open_trade.actual_risk,
                    exit_reason=exit_reason,
                )
            )
            open_trade = None
            continue

        signal_candles = candles[:bar_index]
        result = strategy(signal_candles)
        proposal = result.proposal
        if proposal is None:
            continue
        spread = _spread(bar, settings.typical_spread_price)
        entry = bar.open + spread if proposal.side is Side.LONG else bar.open
        entry += settings.slippage_price if proposal.side is Side.LONG else -settings.slippage_price
        if proposal.side is Side.LONG and not proposal.stop_loss < entry < proposal.take_profit:
            continue
        if proposal.side is Side.SHORT and not proposal.take_profit < entry < proposal.stop_loss:
            continue
        fill_proposal = Proposal(
            side=proposal.side,
            entry=entry,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            reason=proposal.reason,
            indicators=proposal.indicators,
        )
        sized = size_trade(
            equity,
            settings.risk_per_trade_pct,
            proposal,
            spec,
            bid_price=bar.open,
            ask_price=bar.open + spread,
            commission_per_lot_side=settings.commission_per_lot_side,
            slippage_price=settings.slippage_price,
            cost_gate_pct_of_risk=settings.cost_gate_pct_of_risk,
        )
        if sized.reason == "COST_TOO_HIGH":
            skipped_cost += 1
            continue
        if sized.lots is None or sized.actual_risk is None:
            skipped_size += 1
            continue
        open_trade = _OpenTrade(
            proposal=fill_proposal,
            lots=sized.lots,
            actual_risk=sized.actual_risk,
            signal_index=bar_index - 1,
            entry_time=bar.time,
            entry_price=entry,
        )
        exit_event = _exit_for_bar(open_trade, bar, spec, settings)
        if exit_event is not None:
            exit_price, exit_reason = exit_event
            profit = _pnl(open_trade, exit_price, spec, settings)
            equity += profit
            trades.append(
                ReplayTrade(
                    side=open_trade.proposal.side,
                    lots=open_trade.lots,
                    signal_time=candles[open_trade.signal_index].time,
                    entry_time=open_trade.entry_time,
                    exit_time=bar.time,
                    entry_price=open_trade.entry_price,
                    exit_price=exit_price,
                    stop_loss=open_trade.proposal.stop_loss,
                    take_profit=open_trade.proposal.take_profit,
                    actual_risk=open_trade.actual_risk,
                    pnl=profit,
                    result_r=profit / open_trade.actual_risk,
                    exit_reason=exit_reason,
                )
            )
            open_trade = None

    return ReplayResult(
        starting_equity=settings.starting_equity,
        ending_equity=equity,
        trades=tuple(trades),
        open_trade=(
            ReplayPosition(
                side=open_trade.proposal.side,
                lots=open_trade.lots,
                signal_time=candles[open_trade.signal_index].time,
                entry_time=open_trade.entry_time,
                entry_price=open_trade.entry_price,
                stop_loss=open_trade.proposal.stop_loss,
                take_profit=open_trade.proposal.take_profit,
                actual_risk=open_trade.actual_risk,
                candles_held=open_trade.candles_held,
            )
            if open_trade is not None
            else None
        ),
        skipped_cost=skipped_cost,
        skipped_size=skipped_size,
    )
