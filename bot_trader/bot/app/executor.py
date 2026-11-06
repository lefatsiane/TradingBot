from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal

from bot.adapters.broker import (
    Broker,
    BrokerDeal,
    BrokerPosition,
    BrokerResult,
    OrderRequest,
)
from bot.adapters.gateway import BrokerGateway
from bot.adapters.store import AlertPayload, IntentRecord, StateStore, TradeRecord
from bot.core.clock import Clock
from bot.core.types import ExitReason, Side

ResultPolicy = Literal["CONFIRMED", "FAILED", "RETRY", "UNKNOWN"]

RETURN_CODE_POLICY: dict[str | None, ResultPolicy] = {
    "DONE": "CONFIRMED",
    "INVALID_STOPS": "FAILED",
    "INVALID_FILL": "FAILED",
    "REJECTED": "FAILED",
    "NO_MONEY": "FAILED",
    "MARKET_CLOSED": "FAILED",
    "REQUOTE": "RETRY",
    "PRICE_OFF": "RETRY",
    "TIMEOUT": "UNKNOWN",
    "CONNECTION": "UNKNOWN",
    "UNKNOWN": "UNKNOWN",
    "INVALID_VOLUME": "FAILED",
    "INVALID_COMMENT": "FAILED",
    "TRADE_DISABLED": "FAILED",
    "UNKNOWN_SYMBOL": "FAILED",
    "POSITION_NOT_FOUND": "FAILED",
    None: "UNKNOWN",
}


def result_policy(code: str | None) -> ResultPolicy:
    return RETURN_CODE_POLICY.get(code, "UNKNOWN")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent: IntentRecord
    trade: TradeRecord | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    actions: tuple[str, ...]


class Executor:
    def __init__(
        self,
        gateway: BrokerGateway,
        store: StateStore,
        clock: Clock,
        *,
        magic_number: int,
        max_send_retries: int,
        unknown_outcome_wait_s: int,
        code_version: str,
        settings_version: str,
        commission_per_lot_side: dict[str, float] | None = None,
        wait: Callable[[int], None] = time.sleep,
    ) -> None:
        if magic_number <= 0 or max_send_retries not in (0, 1):
            raise ValueError("Invalid executor magic number or send retry limit")
        if unknown_outcome_wait_s < 1:
            raise ValueError("unknown_outcome_wait_s must be positive")
        if not code_version or not settings_version:
            raise ValueError("Executor versions are required")
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._magic_number = magic_number
        self._max_send_retries = max_send_retries
        self._unknown_outcome_wait_s = unknown_outcome_wait_s
        self._code_version = code_version
        self._settings_version = settings_version
        self._commission_per_lot_side = commission_per_lot_side or {}
        self._wait = wait

    def open_trade(
        self,
        *,
        intent_key: str,
        symbol: str,
        side: Side,
        lots: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        actual_risk: float,
        strategy: str,
        timeframe: str,
    ) -> ExecutionResult:
        if (
            not all(isfinite(value) for value in (lots, entry, stop_loss, take_profit, actual_risk))
            or lots <= 0
            or entry <= 0
            or stop_loss <= 0
            or take_profit <= 0
            or actual_risk < 0
            or (
                side is Side.LONG
                and not stop_loss < entry < take_profit
            )
            or (
                side is Side.SHORT
                and not take_profit < entry < stop_loss
            )
        ):
            raise ValueError("Order volume, risk, and protective prices are invalid")
        intent = self._store.create_intent(
            intent_key=f"trade|{strategy}|{timeframe}|{intent_key}",
            symbol=symbol,
            side=side.value,
            lots=lots,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            actual_risk=actual_risk,
            created_at_utc=self._iso_now(),
            code_version=self._code_version,
            settings_version=self._settings_version,
        )
        request = OrderRequest(
            symbol=symbol,
            side=side,
            lots=lots,
            stop_loss=stop_loss,
            take_profit=take_profit,
            magic_number=self._magic_number,
            intent_id=str(intent.intent_id),
        )
        try:
            status, result = self._gateway.run_serialized(
                lambda broker: self._send_locked(broker, intent, request)
            )
        except TimeoutError:
            self._store.transition_intent(intent.intent_id, "FAILED")
            raise
        intent = self._store.get_intent(intent.intent_id)
        if status == "UNKNOWN":
            self._wait(self._unknown_outcome_wait_s)
            try:
                trade = self.resolve_intent(intent)
            except (ConnectionError, TimeoutError):
                return ExecutionResult(
                    self._store.get_intent(intent.intent_id), None, "UNKNOWN_OUTCOME"
                )
            intent = self._store.get_intent(intent.intent_id)
            return ExecutionResult(intent, trade, None if trade else "UNKNOWN_OUTCOME")
        if status == "CONFIRMED" and result is not None and result.ticket is not None:
            trade = self._record_confirmed(
                intent,
                result.ticket,
                strategy,
                timeframe,
            )
            return ExecutionResult(self._store.get_intent(intent.intent_id), trade)
        return ExecutionResult(intent, None, result.code if result else "SEND_FAILED")

    def _send_locked(
        self,
        broker: Broker,
        intent: IntentRecord,
        request: OrderRequest,
    ) -> tuple[ResultPolicy, BrokerResult | None]:
        if self._store.get_flag("stop_new"):
            self._store.transition_intent(intent.intent_id, "FAILED")
            return "FAILED", BrokerResult("STOP_NEW")
        try:
            check = broker.check(request)
        except (ConnectionError, TimeoutError):
            self._store.transition_intent(intent.intent_id, "FAILED")
            return "FAILED", BrokerResult("CHECK_FAILED")
        if check.code != "DONE":
            self._store.transition_intent(intent.intent_id, "FAILED")
            return "FAILED", check
        self._store.transition_intent(intent.intent_id, "SENT")
        for attempt in range(self._max_send_retries + 1):
            try:
                result = broker.send_market(request)
            except (ConnectionError, TimeoutError):
                self._store.transition_intent(intent.intent_id, "UNKNOWN")
                return "UNKNOWN", BrokerResult("UNKNOWN")
            if result is None:
                self._store.transition_intent(intent.intent_id, "UNKNOWN")
                return "UNKNOWN", None
            policy = result_policy(result.code)
            if policy == "RETRY" and attempt < self._max_send_retries:
                try:
                    fresh_check = broker.check(request)
                except (ConnectionError, TimeoutError):
                    self._store.transition_intent(intent.intent_id, "FAILED")
                    return "FAILED", BrokerResult("CHECK_FAILED")
                if fresh_check.code != "DONE":
                    self._store.transition_intent(intent.intent_id, "FAILED")
                    return "FAILED", fresh_check
                continue
            if policy == "CONFIRMED":
                if not result.ticket:
                    self._store.transition_intent(intent.intent_id, "UNKNOWN")
                    return "UNKNOWN", result
                self._store.transition_intent(
                    intent.intent_id, "CONFIRMED", ticket=result.ticket
                )
                return "CONFIRMED", result
            target = "UNKNOWN" if policy in ("UNKNOWN", "RETRY") else "FAILED"
            self._store.transition_intent(intent.intent_id, target)
            return target, result
        raise AssertionError("Send retry loop completed without an outcome")

    def resolve_intent(self, intent: IntentRecord) -> TradeRecord | None:
        """Search broker positions and deals before resolving an uncertain send."""
        if intent.intent_key.startswith("close-"):
            return self._resolve_close_intent(intent, ExitReason.UNKNOWN)
        positions = self._gateway.positions(self._magic_number)
        deals = self._gateway.history_deals(
            datetime.fromisoformat(intent.created_at_utc), self._magic_number
        )
        marker = str(intent.intent_id)
        position = next(
            (
                item
                for item in positions
                if item.ticket == intent.ticket or item.comment == marker
            ),
            None,
        )
        matching_deal = next(
            (
                deal
                for deal in reversed(deals)
                if deal.is_entry is not False
                and (
                    deal.comment == marker
                    or deal.ticket == intent.ticket
                    or deal.position_ticket == intent.ticket
                )
            ),
            None,
        )
        if position is not None:
            ticket = position.ticket
        elif matching_deal is not None:
            ticket = matching_deal.position_ticket
        else:
            self._store.transition_intent(intent.intent_id, "FAILED")
            return None

        current = self._store.get_intent(intent.intent_id)
        if current.status == "PENDING":
            self._store.transition_intent(intent.intent_id, "SENT")
        if current.status != "CONFIRMED":
            self._store.transition_intent(intent.intent_id, "CONFIRMED", ticket=ticket)
        strategy, timeframe = self._intent_context(intent)
        return self._record_confirmed(
            intent,
            ticket,
            strategy=strategy,
            timeframe=timeframe,
        )

    def _record_confirmed(
        self,
        intent: IntentRecord,
        ticket: str,
        strategy: str,
        timeframe: str,
    ) -> TradeRecord:
        existing = next(
            (trade for trade in self._store.open_trades() if trade.ticket == ticket),
            None,
        )
        if existing is not None:
            return existing
        return self._store.create_trade_from_intent(
            intent_id=intent.intent_id,
            ticket=ticket,
            strategy=strategy,
            timeframe=timeframe,
            opened_at_utc=self._iso_now(),
        )

    def close_position(
        self, ticket: str, reason: ExitReason
    ) -> TradeRecord | None:
        positions = self._gateway.positions(self._magic_number)
        position = next((item for item in positions if item.ticket == ticket), None)
        pending_close = next(
            (
                intent
                for intent in self._store.unresolved_intents()
                if intent.intent_key.startswith(f"close-{ticket}-")
            ),
            None,
        )
        if pending_close is not None:
            try:
                resolved = self._resolve_close_intent(pending_close, reason)
            except (ConnectionError, TimeoutError):
                return None
            pending_status = self._store.get_intent(pending_close.intent_id).status
            if pending_status in ("PENDING", "SENT", "UNKNOWN"):
                return None
            if resolved is not None:
                return resolved
            positions = self._gateway.positions(self._magic_number)
            position = next((item for item in positions if item.ticket == ticket), None)
            if position is None:
                return None
        if position is None:
            return None

        close_key = f"close-{ticket}-{self._clock.now_utc().timestamp():.6f}"
        close_intent = self._store.create_intent(
            intent_key=close_key,
            symbol=position.symbol,
            side=(Side.SHORT if position.side is Side.LONG else Side.LONG).value,
            lots=position.lots,
            entry=position.entry,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            actual_risk=0.0,
            created_at_utc=self._iso_now(),
            code_version=self._code_version,
            settings_version=self._settings_version,
        )
        self._store.transition_intent(close_intent.intent_id, "SENT")
        try:
            result = self._gateway.close(ticket)
        except (ConnectionError, TimeoutError):
            self._store.transition_intent(close_intent.intent_id, "UNKNOWN")
            self._wait(self._unknown_outcome_wait_s)
            try:
                return self._resolve_close_intent(close_intent, reason)
            except (ConnectionError, TimeoutError):
                return None
        policy = result_policy(result.code)
        if policy == "UNKNOWN":
            self._store.transition_intent(close_intent.intent_id, "UNKNOWN")
            self._wait(self._unknown_outcome_wait_s)
            try:
                return self._resolve_close_intent(close_intent, reason)
            except (ConnectionError, TimeoutError):
                return None
        if result.code not in ("DONE", "POSITION_NOT_FOUND"):
            self._store.transition_intent(close_intent.intent_id, "FAILED")
            return None
        deal = self._find_close_deal(ticket, close_intent)
        if deal is None:
            self._store.transition_intent(close_intent.intent_id, "UNKNOWN")
            self._wait(self._unknown_outcome_wait_s)
            try:
                return self._resolve_close_intent(close_intent, reason)
            except (ConnectionError, TimeoutError):
                return None
        return self._complete_close(position, close_intent, deal, reason)

    def _resolve_close_intent(
        self, intent: IntentRecord, reason: ExitReason
    ) -> TradeRecord | None:
        ticket = intent.intent_key.split("-", 2)[1]
        positions = self._gateway.positions(self._magic_number)
        position = next((item for item in positions if item.ticket == ticket), None)
        deal = self._find_close_deal(ticket, intent)
        if deal is not None:
            trade = next(
                (item for item in self._store.open_trades() if item.ticket == ticket),
                None,
            )
            if trade is None:
                current = self._store.get_intent(intent.intent_id)
                if current.status in ("SENT", "UNKNOWN"):
                    self._store.transition_intent(
                        intent.intent_id, "CONFIRMED", ticket=deal.ticket
                    )
                return None
            position_for_commission = position
            if position_for_commission is None:
                position_for_commission = BrokerPosition(
                    ticket=trade.ticket,
                    symbol=trade.symbol,
                    side=Side(trade.side),
                    lots=trade.lots,
                    entry=trade.entry,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    magic_number=self._magic_number,
                    comment="",
                    opened_at_utc=datetime.fromisoformat(trade.opened_at_utc),
                )
            return self._complete_close(
                position_for_commission, intent, deal, reason
            )
        current = self._store.get_intent(intent.intent_id)
        if position is not None and current.status in ("SENT", "UNKNOWN"):
            self._store.transition_intent(intent.intent_id, "FAILED")
        return None

    def _complete_close(
        self,
        position: BrokerPosition,
        intent: IntentRecord,
        deal: BrokerDeal,
        reason: ExitReason,
    ) -> TradeRecord | None:
        current = self._store.get_intent(intent.intent_id)
        if current.status in ("SENT", "UNKNOWN"):
            self._store.transition_intent(
                intent.intent_id, "CONFIRMED", ticket=deal.ticket
            )
        trade = next(
            (item for item in self._store.open_trades() if item.ticket == position.ticket),
            None,
        )
        if trade is None:
            return None
        closed = self._store.close_trade(
            position.ticket,
            deal.time_utc.isoformat(),
            deal.price,
            deal.profit,
            deal.commission,
            reason,
        )
        self._check_commission_drift(position.symbol, position.lots, deal.commission)
        return closed

    def reconcile(self) -> ReconcileReport:
        actions: list[str] = []
        positions = self._gateway.positions(self._magic_number)
        trades = {trade.ticket: trade for trade in self._store.open_trades()}
        positions_by_ticket = {position.ticket: position for position in positions}
        for position in positions:
            trade = trades.get(position.ticket)
            if trade is not None:
                if position.stop_loss <= 0:
                    if trade.stop_loss > 0:
                        result = self._gateway.modify_sl_tp(
                            position.ticket, trade.stop_loss, trade.take_profit
                        )
                        if result.code != "DONE":
                            self.close_position(position.ticket, ExitReason.UNKNOWN)
                            actions.append(f"closed-unprotected:{position.ticket}")
                        else:
                            actions.append(f"restored-stop:{position.ticket}")
                    else:
                        self.close_position(position.ticket, ExitReason.UNKNOWN)
                        actions.append(f"closed-unprotected:{position.ticket}")
                continue
            if position.stop_loss <= 0:
                intent = self._intent_from_position_comment(position)
                if intent is not None and intent.stop_loss > 0:
                    result = self._gateway.modify_sl_tp(
                        position.ticket, intent.stop_loss, intent.take_profit
                    )
                    if result.code == "DONE":
                        actions.append(f"restored-stop:{position.ticket}")
                        position = replace(
                            position,
                            stop_loss=intent.stop_loss,
                            take_profit=intent.take_profit,
                        )
                    else:
                        self.close_position(position.ticket, ExitReason.UNKNOWN)
                        actions.append(f"closed-unprotected:{position.ticket}")
                        continue
                else:
                    self.close_position(position.ticket, ExitReason.UNKNOWN)
                    actions.append(f"closed-unprotected:{position.ticket}")
                    continue
            self._adopt_position(position)
            actions.append(f"adopted:{position.ticket}")

        for ticket, trade in trades.items():
            if ticket in positions_by_ticket:
                continue
            deals = self._gateway.history_deals(
                datetime.fromisoformat(trade.opened_at_utc), self._magic_number
            )
            close_deal = next(
                (
                    deal
                    for deal in reversed(deals)
                    if deal.position_ticket == ticket
                    and deal.is_entry is not True
                    and deal.time_utc.isoformat() >= trade.opened_at_utc
                ),
                None,
            )
            if close_deal is not None:
                self._store.close_trade(
                    ticket,
                    close_deal.time_utc.isoformat(),
                    close_deal.price,
                    close_deal.profit,
                    close_deal.commission,
                    ExitReason.UNKNOWN,
                )
                actions.append(f"closed-in-store:{ticket}")
        return ReconcileReport(tuple(actions))

    def _intent_from_position_comment(
        self, position: BrokerPosition
    ) -> IntentRecord | None:
        if not position.comment.isdigit():
            return None
        try:
            return self._store.get_intent(int(position.comment))
        except KeyError:
            return None

    @staticmethod
    def _intent_context(intent: IntentRecord) -> tuple[str, str]:
        parts = intent.intent_key.split("|", 3)
        if len(parts) == 4 and parts[0] == "trade":
            return parts[1], parts[2]
        return "UNKNOWN", "H1"

    def _adopt_position(self, position: BrokerPosition) -> None:
        key = f"adopt-{position.ticket}"
        existing = self._store.find_intent_by_key(key)
        if existing is None:
            intent = self._store.create_intent(
                intent_key=key,
                symbol=position.symbol,
                side=position.side.value,
                lots=position.lots,
                entry=position.entry,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                actual_risk=0.0,
                created_at_utc=position.opened_at_utc.isoformat(),
                code_version=self._code_version,
                settings_version=self._settings_version,
            )
        else:
            intent = existing
        if intent.status == "PENDING":
            self._store.transition_intent(intent.intent_id, "SENT")
        if intent.status in ("PENDING", "SENT", "UNKNOWN"):
            self._store.transition_intent(
                intent.intent_id, "CONFIRMED", ticket=position.ticket
            )
        self._store.create_trade_from_intent(
            intent_id=intent.intent_id,
            ticket=position.ticket,
            strategy="ADOPTED",
            timeframe="H1",
            opened_at_utc=position.opened_at_utc.isoformat(),
        )
        self._store.enqueue_alert(
            AlertPayload(
                self._iso_now(),
                "WARNING",
                f"Adopted broker position {position.ticket} ({position.symbol})",
            )
        )

    def _find_close_deal(
        self, ticket: str, close_intent: IntentRecord
    ) -> BrokerDeal | None:
        deals = self._gateway.history_deals(
            datetime.fromisoformat(close_intent.created_at_utc),
            self._magic_number,
        )
        return next(
            (
                deal
                for deal in reversed(deals)
                if deal.position_ticket == ticket
                and deal.is_entry is not True
                and deal.time_utc.isoformat() >= close_intent.created_at_utc
            ),
            None,
        )

    def _check_commission_drift(
        self, symbol: str, lots: float, charged_commission: float
    ) -> None:
        configured = self._commission_per_lot_side.get(symbol)
        if configured is None:
            return
        expected = configured * lots
        charged = abs(charged_commission)
        drift = abs(charged - expected)
        if drift > abs(expected) * 0.2 and drift > 1e-9:
            self._store.enqueue_alert(
                AlertPayload(
                    self._iso_now(),
                    "WARNING",
                    f"Commission drift for {symbol}: expected {expected:.2f}, "
                    f"charged {charged:.2f}",
                )
            )

    def _iso_now(self) -> str:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Executor clock must return timezone-aware UTC")
        return now.astimezone(UTC).isoformat()
