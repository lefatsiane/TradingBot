from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from bot.adapters.broker import (
    BrokerAccount,
    BrokerDeal,
    BrokerPosition,
    BrokerResult,
    BrokerTerminalInfo,
    OrderRequest,
    Tick,
)
from bot.core.clock import SimClock
from bot.core.types import Candle, Side, SymbolSpec

FaultName = Literal[
    "timeout_unfilled",
    "timeout_filled",
    "dropped_response",
    "crash_after_send",
    "invalid_stops",
    "disconnect",
    "stale_tick",
    "wrong_fill_mode",
    "duplicate_candle",
]
FaultOperation = Literal["order", "tick", "candles"]


@dataclass(frozen=True, slots=True)
class Fault:
    call_number: int
    name: FaultName
    operation: FaultOperation = "order"

    def __post_init__(self) -> None:
        if self.call_number < 1:
            raise ValueError("Fault call_number must be positive")
        if self.name == "stale_tick" and self.operation != "tick":
            raise ValueError("stale_tick faults must target tick calls")
        if self.name == "duplicate_candle" and self.operation != "candles":
            raise ValueError("duplicate_candle faults must target candle calls")
        if self.name not in ("stale_tick", "duplicate_candle") and self.operation != "order":
            raise ValueError(f"{self.name} faults must target order calls")


@dataclass(frozen=True, slots=True)
class FaultPlan:
    faults: tuple[Fault, ...] = ()

    def __post_init__(self) -> None:
        calls = [(fault.operation, fault.call_number) for fault in self.faults]
        if len(calls) != len(set(calls)):
            raise ValueError("Only one injected fault is allowed per call")

    def at_call(self, operation: FaultOperation, call_number: int) -> FaultName | None:
        return next(
            (
                fault.name
                for fault in self.faults
                if fault.operation == operation and fault.call_number == call_number
            ),
            None,
        )


class SimulatedCrash(RuntimeError):
    """A deterministic crash injected after the simulated broker accepts an order."""


@dataclass(frozen=True, slots=True)
class SimCosts:
    commission_per_lot_side: float = 0.0
    slippage_points: float = 0.0

    def __post_init__(self) -> None:
        if (
            not isfinite(self.commission_per_lot_side)
            or self.commission_per_lot_side < 0
            or not isfinite(self.slippage_points)
            or self.slippage_points < 0
        ):
            raise ValueError("Simulated costs must be finite and non-negative")


@dataclass(slots=True)
class SimBroker:
    account_snapshot: BrokerAccount
    specs: dict[str, SymbolSpec]
    tick_data: dict[str, Tick]
    clock: SimClock
    candle_data: dict[tuple[str, str], tuple[Candle, ...]] = field(default_factory=dict)
    fault_plan: FaultPlan = FaultPlan()
    connected: bool = True
    server_offset_hours: int = 3
    terminal_name: str = "SimBroker"
    company: str = "Simulated"
    costs: dict[str, SimCosts] = field(default_factory=dict)
    session_hours: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _positions: dict[str, BrokerPosition] = field(default_factory=dict, init=False)
    _deals: list[BrokerDeal] = field(default_factory=list, init=False)
    _last_candle_times: dict[str, datetime] = field(default_factory=dict, init=False)
    _order_calls: int = field(default=0, init=False)
    _tick_calls: int = field(default=0, init=False)
    _candle_calls: int = field(default=0, init=False)
    _next_ticket: int = field(default=1, init=False)
    _next_deal: int = field(default=1, init=False)
    _balance: float = field(default=0.0, init=False)

    def connect(self) -> bool:
        self.connected = True
        return True

    def __post_init__(self) -> None:
        if not self.account_snapshot.is_demo:
            raise ValueError("The simulated broker accepts demo-account snapshots only")
        if not -12 <= self.server_offset_hours <= 14:
            raise ValueError("server_offset_hours must be in range -12..14")
        unknown_cost_symbols = set(self.costs) - set(self.specs)
        if unknown_cost_symbols:
            raise ValueError(
                f"Costs configured for unknown symbols: {', '.join(sorted(unknown_cost_symbols))}"
            )
        unknown_session_symbols = set(self.session_hours) - set(self.specs)
        if unknown_session_symbols:
            raise ValueError(
                "Sessions configured for unknown symbols: "
                f"{', '.join(sorted(unknown_session_symbols))}"
            )
        self._balance = self.account_snapshot.balance

    def account(self) -> BrokerAccount:
        self._require_connection()
        return replace(
            self.account_snapshot,
            balance=self._balance,
            equity=self._equity(),
        )

    def terminal_info(self) -> BrokerTerminalInfo:
        return BrokerTerminalInfo(
            connected=self.connected,
            trade_allowed=self.account_snapshot.trade_allowed,
            name=self.terminal_name,
            company=self.company,
        )

    def symbol_info(self, symbol: str) -> SymbolSpec:
        self._require_connection()
        try:
            return self.specs[symbol]
        except KeyError as error:
            raise ValueError(f"Unknown simulated symbol: {symbol}") from error

    def symbol_sessions_utc(self, symbol: str) -> tuple[str, ...]:
        self._require_connection()
        try:
            return self.session_hours[symbol]
        except KeyError as error:
            raise ValueError(f"No simulated sessions configured for {symbol}") from error

    def candles(self, symbol: str, timeframe: str) -> tuple[Candle, ...]:
        self._require_connection()
        self._candle_calls += 1
        candles = self.candle_data.get((symbol, timeframe), ())
        fault = self.fault_plan.at_call("candles", self._candle_calls)
        if fault == "duplicate_candle" and candles:
            return (*candles, candles[-1])
        return candles

    def tick(self, symbol: str) -> Tick | None:
        self._require_connection()
        self._tick_calls += 1
        fault = self.fault_plan.at_call("tick", self._tick_calls)
        if fault == "stale_tick":
            return None
        return self.tick_data.get(symbol)

    def server_utc_offset_hours(self) -> int:
        self._require_connection()
        return self.server_offset_hours

    def calculate_margin(self, request: OrderRequest) -> float | None:
        self._require_connection()
        tick = self.tick_data.get(request.symbol)
        if tick is None:
            return None
        spec = self.symbol_info(request.symbol)
        entry = tick.ask if request.side is Side.LONG else tick.bid
        return spec.contract_size * request.lots * entry / self.account_snapshot.leverage

    def positions(self, magic_number: int) -> tuple[BrokerPosition, ...]:
        self._require_connection()
        return tuple(
            position
            for position in self._positions.values()
            if position.magic_number == magic_number
        )

    def history_deals(self, since_utc: datetime, magic_number: int) -> tuple[BrokerDeal, ...]:
        self._require_connection()
        if since_utc.tzinfo is None or since_utc.utcoffset() is None:
            raise ValueError("since_utc must be timezone-aware")
        since = since_utc.astimezone(UTC)
        return tuple(
            deal
            for deal in self._deals
            if deal.magic_number == magic_number and deal.time_utc >= since
        )

    def check(self, request: OrderRequest) -> BrokerResult:
        self._require_connection()
        validation = self._validate_request(request)
        return validation or BrokerResult("DONE")

    def send_market(self, request: OrderRequest) -> BrokerResult | None:
        self._require_connection()
        self._order_calls += 1
        fault = self.fault_plan.at_call("order", self._order_calls)
        if fault == "disconnect":
            self.connected = False
            return BrokerResult("CONNECTION", message="Injected disconnect")
        if fault == "timeout_unfilled":
            return None
        if fault == "invalid_stops":
            return BrokerResult("INVALID_STOPS", message="Injected invalid stops")
        if fault == "wrong_fill_mode":
            return BrokerResult("INVALID_FILL", message="Injected unsupported fill mode")

        validation = self._validate_request(request)
        if validation is not None:
            return validation
        accepted = self._accept_order(request)

        if fault in ("timeout_filled", "dropped_response"):
            return None
        if fault == "crash_after_send":
            raise SimulatedCrash("Injected crash after order acceptance")
        return accepted

    def modify_sl_tp(self, ticket: str, stop_loss: float, take_profit: float) -> BrokerResult:
        self._require_connection()
        position = self._positions.get(ticket)
        if position is None:
            return BrokerResult("POSITION_NOT_FOUND")
        valid = (
            stop_loss < position.entry < take_profit
            if position.side is Side.LONG
            else take_profit < position.entry < stop_loss
        )
        if not valid:
            return BrokerResult("INVALID_STOPS")
        self._positions[ticket] = replace(
            position, stop_loss=stop_loss, take_profit=take_profit
        )
        return BrokerResult("DONE", ticket=ticket)

    def close(self, ticket: str) -> BrokerResult:
        self._require_connection()
        position = self._positions.pop(ticket, None)
        if position is None:
            return BrokerResult("POSITION_NOT_FOUND")
        tick = self.tick_data.get(position.symbol)
        if tick is None:
            self._positions[ticket] = position
            return BrokerResult("PRICE_OFF", message="No simulated tick")
        costs = self.costs.get(position.symbol, SimCosts())
        spec = self.specs[position.symbol]
        slippage = costs.slippage_points * spec.point
        exit_price = (
            tick.bid - slippage
            if position.side is Side.LONG
            else tick.ask + slippage
        )
        return self._close_at(position, exit_price, self.clock.now_utc())

    def process_candle(self, symbol: str, candle: Candle) -> tuple[BrokerResult, ...]:
        """Apply candle high/low protective-stop fills, resolving dual hits stop-first."""
        self._require_connection()
        if symbol not in self.specs:
            raise ValueError(f"Unknown simulated symbol: {symbol}")
        if candle.time < self.clock.now_utc():
            raise ValueError("Simulated candle time cannot precede the simulator clock")
        previous_time = self._last_candle_times.get(symbol)
        if previous_time is not None and candle.time <= previous_time:
            raise ValueError("Simulated candles must be strictly increasing and unique")
        self._last_candle_times[symbol] = candle.time
        self.clock.set(candle.time)
        spec = self.specs[symbol]
        costs = self.costs.get(symbol, SimCosts())
        current_tick = self.tick_data.get(symbol)
        spread = candle.spread
        if spread == 0 and current_tick is not None:
            spread = current_tick.ask - current_tick.bid
        slippage = costs.slippage_points * spec.point
        results: list[BrokerResult] = []
        for ticket, position in tuple(self._positions.items()):
            if position.symbol != symbol or candle.time < position.opened_at_utc:
                continue
            if position.side is Side.LONG:
                stop_hit = candle.low <= position.stop_loss
                target_hit = candle.high >= position.take_profit
                if stop_hit:
                    price = min(candle.open, position.stop_loss) - slippage
                elif target_hit:
                    price = max(candle.open, position.take_profit) - slippage
                else:
                    continue
            else:
                ask_open = candle.open + spread
                stop_hit = candle.high + spread >= position.stop_loss
                target_hit = candle.low + spread <= position.take_profit
                if stop_hit:
                    price = max(ask_open, position.stop_loss) + slippage
                elif target_hit:
                    price = min(ask_open, position.take_profit) + slippage
                else:
                    continue
            results.append(self._close_at(position, price, candle.time))
        return tuple(results)

    @property
    def order_calls(self) -> int:
        return self._order_calls

    def _accept_order(self, request: OrderRequest) -> BrokerResult:
        tick = self.tick_data.get(request.symbol)
        if tick is None:
            return BrokerResult("PRICE_OFF", message="No simulated tick")
        spec = self.specs[request.symbol]
        costs = self.costs.get(request.symbol, SimCosts())
        slippage = costs.slippage_points * spec.point
        entry = (
            tick.ask + slippage
            if request.side is Side.LONG
            else tick.bid - slippage
        )
        if request.side is Side.LONG and not request.stop_loss < entry < request.take_profit:
            return BrokerResult("INVALID_STOPS")
        if request.side is Side.SHORT and not request.take_profit < entry < request.stop_loss:
            return BrokerResult("INVALID_STOPS")
        if request.lots < spec.volume_min or request.lots > spec.volume_max:
            return BrokerResult("INVALID_VOLUME")
        if not self._is_volume_step(request.lots, spec.volume_min, spec.volume_step):
            return BrokerResult("INVALID_VOLUME")
        ticket = str(self._next_ticket)
        self._next_ticket += 1
        commission = costs.commission_per_lot_side * request.lots
        self._balance -= commission
        position = BrokerPosition(
            ticket=ticket,
            symbol=request.symbol,
            side=request.side,
            lots=request.lots,
            entry=entry,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            magic_number=request.magic_number,
            comment=request.intent_id[:31],
            opened_at_utc=self.clock.now_utc(),
            entry_commission=commission,
        )
        self._positions[ticket] = position
        self._record_deal(
            position,
            price=entry,
            profit=0.0,
            commission=commission,
            time_utc=position.opened_at_utc,
            is_entry=True,
        )
        return BrokerResult(
            "DONE",
            ticket=ticket,
            price=entry,
            lots=request.lots,
            commission=commission,
        )

    def _close_at(
        self, position: BrokerPosition, price: float, time_utc: datetime
    ) -> BrokerResult:
        if time_utc.tzinfo is None or time_utc.utcoffset() is None:
            raise ValueError("Simulated deal time must be timezone-aware")
        costs = self.costs.get(position.symbol, SimCosts())
        commission = costs.commission_per_lot_side * position.lots
        spec = self.specs[position.symbol]
        direction = 1.0 if position.side is Side.LONG else -1.0
        profit = (
            (price - position.entry)
            / spec.tick_size
            * spec.tick_value_loss
            * position.lots
            * direction
        )
        self._balance += profit - commission
        self._positions.pop(position.ticket, None)
        self._record_deal(
            position,
            price=price,
            profit=profit,
            commission=commission,
            time_utc=time_utc,
            is_entry=False,
        )
        return BrokerResult(
            "DONE",
            ticket=position.ticket,
            price=price,
            lots=position.lots,
            commission=commission,
        )

    def _record_deal(
        self,
        position: BrokerPosition,
        *,
        price: float,
        profit: float,
        commission: float,
        time_utc: datetime,
        is_entry: bool,
    ) -> None:
        self._deals.append(
            BrokerDeal(
                ticket=str(self._next_deal),
                position_ticket=position.ticket,
                symbol=position.symbol,
                side=position.side,
                lots=position.lots,
                price=price,
                time_utc=time_utc.astimezone(UTC),
                profit=profit,
                commission=commission,
                magic_number=position.magic_number,
                comment=position.comment,
                is_entry=is_entry,
            )
        )
        self._next_deal += 1

    def _equity(self) -> float:
        equity = self._balance
        for position in self._positions.values():
            tick = self.tick_data.get(position.symbol)
            if tick is None:
                continue
            spec = self.specs[position.symbol]
            costs = self.costs.get(position.symbol, SimCosts())
            price = (
                tick.bid - costs.slippage_points * spec.point
                if position.side is Side.LONG
                else tick.ask + costs.slippage_points * spec.point
            )
            direction = 1.0 if position.side is Side.LONG else -1.0
            equity += (
                (price - position.entry)
                / spec.tick_size
                * spec.tick_value_loss
                * position.lots
                * direction
                - costs.commission_per_lot_side * position.lots
            )
        return equity

    def _validate_request(self, request: OrderRequest) -> BrokerResult | None:
        if not self.account_snapshot.trade_allowed:
            return BrokerResult("TRADE_DISABLED")
        if request.symbol not in self.specs:
            return BrokerResult("UNKNOWN_SYMBOL")
        if request.lots <= 0:
            return BrokerResult("INVALID_VOLUME")
        if not request.intent_id:
            return BrokerResult("INVALID_COMMENT")
        tick = self.tick_data.get(request.symbol)
        if tick is None or tick.bid <= 0 or tick.ask < tick.bid:
            return BrokerResult("PRICE_OFF")
        if request.side is Side.LONG and not request.stop_loss < tick.ask < request.take_profit:
            return BrokerResult("INVALID_STOPS")
        if request.side is Side.SHORT and not request.take_profit < tick.bid < request.stop_loss:
            return BrokerResult("INVALID_STOPS")
        spec = self.specs[request.symbol]
        if request.lots < spec.volume_min or request.lots > spec.volume_max:
            return BrokerResult("INVALID_VOLUME")
        if not self._is_volume_step(request.lots, spec.volume_min, spec.volume_step):
            return BrokerResult("INVALID_VOLUME")
        return None

    @staticmethod
    def _is_volume_step(volume: float, minimum: float, step: float) -> bool:
        steps = (volume - minimum) / step
        return abs(steps - round(steps)) < 1e-8

    def _require_connection(self) -> None:
        if not self.connected:
            raise ConnectionError("Simulated broker is disconnected")
