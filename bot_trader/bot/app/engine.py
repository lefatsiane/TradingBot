from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import BinaryIO, Literal

from bot.adapters.broker import OrderRequest, Tick
from bot.adapters.gateway import BrokerGateway
from bot.adapters.store import AlertPayload, DecisionRecord, StateStore
from bot.app.config import Settings, parse_sessions, startup_guard
from bot.app.data import DataQualityError, last_closed_candles
from bot.app.executor import Executor
from bot.app.news import (
    NewsEvent,
    calendar_has_minimum_coverage,
    is_news_blocked,
    load_news_calendar,
)
from bot.core.clock import Clock
from bot.core.gates import (
    GateDecision,
    SessionWindow,
    evaluate_entry_gates,
    next_weekend_close_utc,
)
from bot.core.indicators import atr
from bot.core.risk import (
    RiskLimits,
    entry_slot_reason,
    evaluate_entry,
    evaluate_loss_limits,
)
from bot.core.sizing import SizeResult, size_trade
from bot.core.strategy_a import propose as propose_a
from bot.core.strategy_b import propose as propose_b
from bot.core.timeutil import (
    month_start_utc,
    next_trading_day_boundary_utc,
    trading_day_start_utc,
)
from bot.core.types import (
    Candle,
    Decision,
    ExitReason,
    OpenRisk,
    Proposal,
    ReasonCode,
    Side,
    StrategyResult,
    SymbolSpec,
)

MAX_ENTRY_TICK_AGE = timedelta(minutes=10)


def _tick_is_fresh(tick: Tick, at_utc: datetime) -> bool:
    return (
        tick.time_utc.tzinfo is not None
        and tick.time_utc.utcoffset() is not None
        and tick.time_utc <= at_utc
        and at_utc - tick.time_utc <= MAX_ENTRY_TICK_AGE
        and all(isfinite(value) for value in (tick.bid, tick.ask))
        and tick.bid > 0
        and tick.ask >= tick.bid
    )


@dataclass(frozen=True, slots=True)
class ManagementReport:
    actions: tuple[str, ...]
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class EntryContext:
    settings: Settings
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    now_utc: datetime
    spec: SymbolSpec
    tick: Tick | None
    specs: dict[str, SymbolSpec]
    open_risks: tuple[OpenRisk, ...] = ()
    pending_risks: tuple[OpenRisk, ...] = ()
    bars_since_stop: int | None = None
    margin_used_after_pct: float = 0.0
    realized_pnl_today: float = 0.0
    start_of_day_equity: float = 0.0
    equity: float = 0.0
    rollover_at_utc: datetime | None = None
    weekend_close_utc: datetime | None = None
    news_events: tuple[NewsEvent, ...] = ()
    news_calendar_valid: bool = True
    strategy_mode: Literal["a", "b", "both"] = "both"
    stop_new: bool = False
    halted_daily: bool = False
    halted_monthly: bool = False
    halted_error: bool = False
    margin_calculation_failed: bool = False
    data_error: bool = False
    check_passed: bool = True


def evaluate_candle(context: EntryContext) -> Decision:
    """Evaluate one already-closed candle without broker, clock, or store I/O."""
    at_utc = _as_utc(context.now_utc)
    if context.stop_new:
        return _decision(context, at_utc, ReasonCode.STOP_NEW)
    if context.halted_daily:
        return _decision(context, at_utc, ReasonCode.DAILY_HALT)
    if context.halted_monthly:
        return _decision(context, at_utc, ReasonCode.MONTHLY_HALT)
    if context.halted_error:
        return _decision(context, at_utc, ReasonCode.ERROR_HALT)

    weekend_close = context.weekend_close_utc
    if weekend_close is None:
        return _decision(context, at_utc, ReasonCode.DATA_ERROR)
    schedule = parse_sessions(context.settings.symbols[context.symbol].hours_utc)
    gate = _gate(
        context,
        at_utc,
        schedule,
        weekend_close,
        rollover_at_utc=at_utc - timedelta(days=2),
        spread_price=0.0,
        candle_range=0.0,
        atr_value=1.0,
        news_calendar_valid=True,
    )
    if gate.reason is ReasonCode.WEEKEND_WINDOW:
        return _decision(context, at_utc, gate.reason)
    if (
        context.data_error
        or not context.candles
        or context.tick is None
        or not _tick_is_fresh(context.tick, at_utc)
    ):
        return _decision(context, at_utc, ReasonCode.DATA_ERROR)
    rollover_at_utc = context.rollover_at_utc
    if (
        rollover_at_utc is None
        or context.equity <= 0
        or context.start_of_day_equity <= 0
    ):
        return _decision(context, at_utc, ReasonCode.DATA_ERROR)
    symbol_settings = context.settings.symbols[context.symbol]
    if not symbol_settings.enabled:
        return _decision(context, at_utc, ReasonCode.SYMBOL_DISABLED)
    if gate.reason is not None:
        return _decision(context, at_utc, gate.reason)
    if not context.news_calendar_valid:
        return _decision(context, at_utc, ReasonCode.NEWS_CALENDAR_STALE)
    if is_news_blocked(
        context.symbol,
        at_utc,
        context.news_events,
        before_min=context.settings.news.block_before_min,
        after_min=context.settings.news.block_after_min,
    ):
        return _decision(context, at_utc, ReasonCode.NEWS_WINDOW)

    atr_values = atr(context.candles, context.settings.strategy_a.atr_period)
    current_atr = atr_values[-1] if atr_values else 0.0
    current_candle = context.candles[-1]
    full_gate = _gate(
        context,
        at_utc,
        schedule,
        weekend_close,
        rollover_at_utc=rollover_at_utc,
        spread_price=context.tick.ask - context.tick.bid,
        candle_range=current_candle.high - current_candle.low,
        atr_value=current_atr,
        news_calendar_valid=True,
    )
    if full_gate.reason is not None:
        return _decision(context, at_utc, full_gate.reason)

    slot_reason = entry_slot_reason(
        symbol=context.symbol,
        positions=context.open_risks,
        pending=context.pending_risks,
        bars_since_stop=context.bars_since_stop,
        limits=_risk_limits(context.settings),
    )
    if slot_reason is not None:
        return _decision(context, at_utc, slot_reason)

    strategy, result = _propose(context)
    if result.proposal is None:
        return _decision(
            context,
            at_utc,
            result.reason or ReasonCode.NO_TRIGGER,
            strategy=strategy,
            indicators=result.indicators,
        )
    sizing = _size(context, result.proposal, symbol_settings.commission_per_lot_side)
    if sizing.reason is not None:
        return _decision(
            context,
            at_utc,
            _reason(sizing.reason),
            strategy=strategy,
            proposal=result.proposal,
            indicators=result.indicators,
        )
    if sizing.lots is None or sizing.actual_risk is None:
        return _decision(context, at_utc, ReasonCode.BELOW_MIN_LOT)
    if context.margin_calculation_failed:
        return _decision(
            context,
            at_utc,
            ReasonCode.NO_MARGIN,
            strategy=strategy,
            proposal=result.proposal,
            indicators=result.indicators,
        )

    risk = evaluate_entry(
        symbol=context.symbol,
        side=result.proposal.side,
        new_risk_pct=sizing.actual_risk / context.equity * 100.0,
        positions=context.open_risks,
        pending=context.pending_risks,
        specs=context.specs,
        realized_pnl_today=context.realized_pnl_today,
        start_of_day_equity=context.start_of_day_equity,
        margin_used_after_pct=context.margin_used_after_pct,
        bars_since_stop=context.bars_since_stop,
        limits=_risk_limits(context.settings),
    )
    if not risk.allowed:
        return _decision(
            context,
            at_utc,
            _reason(risk.reason),
            strategy=strategy,
            proposal=result.proposal,
            indicators=result.indicators,
        )
    if not context.check_passed:
        return _decision(
            context,
            at_utc,
            ReasonCode.CHECK_FAILED,
            strategy=strategy,
            proposal=result.proposal,
            indicators=result.indicators,
        )
    return _decision(
        context,
        at_utc,
        ReasonCode.ENTRY_ALLOWED,
        strategy=strategy,
        proposal=result.proposal,
        indicators=result.indicators,
    )


def _gate(
    context: EntryContext,
    at_utc: datetime,
    sessions: tuple[SessionWindow, ...],
    weekend_close: datetime,
    *,
    rollover_at_utc: datetime,
    spread_price: float,
    candle_range: float,
    atr_value: float,
    news_calendar_valid: bool,
) -> GateDecision:
    return evaluate_entry_gates(
        at_utc=at_utc,
        symbol_enabled=True,
        sessions=sessions,
        spread_price=spread_price,
        point=context.spec.point,
        max_spread_points=context.settings.symbols[context.symbol].max_spread_points,
        candle_range=candle_range,
        atr=atr_value,
        max_candle_range_atr=context.settings.gates.max_candle_range_atr,
        rollover_at_utc=rollover_at_utc,
        rollover_block_before_min=context.settings.timing.rollover_block_before_min,
        rollover_block_after_min=context.settings.timing.rollover_block_after_min,
        news_calendar_valid=news_calendar_valid,
        news_blocked=False,
        weekend_close_utc=weekend_close,
        no_new_entries_before_weekend_close_min=(
            context.settings.timing.no_new_entries_before_weekend_close_min
        ),
        flatten_before_weekend_close_min=(
            context.settings.timing.flatten_before_weekend_close_min
        ),
    )


def _propose(context: EntryContext) -> tuple[str, StrategyResult]:
    if context.strategy_mode == "a":
        if context.timeframe in context.settings.strategy_a.timeframes:
            return "A", propose_a(context.candles, context.settings.strategy_a.to_params())
        return "A", StrategyResult(None, ReasonCode.WARMUP)
    if context.strategy_mode == "b":
        if context.timeframe == "H1":
            return (
                "B",
                propose_b(
                    context.candles,
                    context.settings.strategy_b.to_params(),
                    timeframe=context.timeframe,
                ),
            )
        return "B", StrategyResult(None, ReasonCode.WARMUP)
    if context.timeframe in context.settings.strategy_a.timeframes:
        result_a = propose_a(context.candles, context.settings.strategy_a.to_params())
        if result_a.proposal is not None or context.timeframe != "H1":
            return "A", result_a
        result_b = propose_b(
            context.candles,
            context.settings.strategy_b.to_params(),
            timeframe=context.timeframe,
        )
        if result_b.proposal is not None:
            return "B", result_b
        return "A", result_a
    if context.timeframe == "H1":
        return (
            "B",
            propose_b(
                context.candles,
                context.settings.strategy_b.to_params(),
                timeframe=context.timeframe,
            ),
        )
    return "A", StrategyResult(None, ReasonCode.WARMUP)


def _size(
    context: EntryContext,
    proposal: Proposal,
    commission_per_lot_side: float,
) -> SizeResult:
    return size_trade(
        context.equity,
        context.settings.risk.risk_per_trade_pct,
        proposal,
        context.spec,
        bid_price=context.tick.bid,
        ask_price=context.tick.ask,
        commission_per_lot_side=commission_per_lot_side,
        slippage_price=(
            context.settings.symbols[context.symbol].slippage_points
            * context.spec.point
        ),
        cost_gate_pct_of_risk=context.settings.risk.cost_gate_pct_of_risk,
    )


def _risk_limits(settings: Settings) -> RiskLimits:
    return RiskLimits(
        daily_loss_pct=settings.risk.daily_loss_pct,
        monthly_loss_pct=settings.risk.monthly_loss_pct,
        max_net_currency_exposure_pct=settings.risk.max_net_currency_exposure_pct,
        max_open_trades=settings.risk.max_open_trades,
        max_margin_used_pct=settings.risk.max_margin_used_pct,
        cooldown_bars_after_stop=settings.risk.cooldown_bars_after_stop,
    )


def _include_lane_risks(
    current: tuple[OpenRisk, ...],
    lane_risks: list[OpenRisk],
) -> tuple[OpenRisk, ...]:
    known_symbols = {risk.symbol for risk in current}
    additions = tuple(
        risk for risk in lane_risks if risk.symbol not in known_symbols
    )
    return (*current, *additions)


def _decision(
    context: EntryContext,
    at_utc: datetime,
    reason: ReasonCode,
    *,
    strategy: str | None = None,
    proposal: Proposal | None = None,
    indicators: tuple[tuple[str, float], ...] = (),
) -> Decision:
    return Decision(
        symbol=context.symbol,
        time=at_utc,
        reason=reason,
        strategy=strategy,
        proposal=proposal,
        indicators=indicators,
    )


def _reason(reason: str | None) -> ReasonCode:
    if reason is None:
        return ReasonCode.CHECK_FAILED
    try:
        return ReasonCode(reason)
    except ValueError:
        return ReasonCode.CHECK_FAILED


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return value.astimezone(UTC)


class Engine:
    def __init__(
        self,
        settings: Settings,
        gateway: BrokerGateway,
        store: StateStore,
        executor: Executor,
        clock: Clock,
        context_factory: Callable[[str, str, tuple[Candle, ...]], EntryContext] | None = None,
        *,
        code_version: str,
        settings_version: str,
        news_enabled: bool = True,
        strategy_mode: Literal["a", "b", "both"] = "both",
    ) -> None:
        if strategy_mode not in ("a", "b", "both"):
            raise ValueError("strategy_mode must be 'a', 'b', or 'both'")
        self._settings = settings
        self._gateway = gateway
        self._store = store
        self._executor = executor
        self._clock = clock
        self._context_factory = context_factory or self._build_entry_context
        self._code_version = code_version
        self._settings_version = settings_version
        self._news_enabled = news_enabled
        self._strategy_mode = strategy_mode
        self._news_events: tuple[NewsEvent, ...] = ()
        self._news_calendar_valid: bool | None = None
        self._instance_lock_file: BinaryIO | None = None

    def run_entry_lane(self) -> tuple[Decision, ...]:
        decisions: list[Decision] = []
        accepted_risks: list[OpenRisk] = []
        pending_risks: list[OpenRisk] = []
        if self._news_calendar_valid is not None:
            self._initialize_news_calendar()
        if self._gateway.degraded or self._store.get_meta("gateway_degraded") == "1":
            return ()
        try:
            for symbol, symbol_settings in self._settings.symbols.items():
                if not symbol_settings.enabled:
                    continue
                timeframes = (
                    self._settings.strategy_a.timeframes
                    if self._strategy_mode == "a"
                    else ("H1",)
                    if self._strategy_mode == "b"
                    else tuple(
                        dict.fromkeys(
                            (*self._settings.strategy_a.timeframes, "H1")
                        )
                    )
                )
                for timeframe in timeframes:
                    now = _as_utc(self._clock.now_utc())
                    try:
                        raw = self._gateway.candles(symbol, timeframe)
                        closed = last_closed_candles(raw, timeframe, now_utc=now)
                    except DataQualityError as error:
                        alert_key = f"entry_data_error_alerted:{symbol}:{timeframe}"
                        if self._store.get_meta(alert_key) != "1":
                            self._store.enqueue_alert(
                                AlertPayload(
                                    now.isoformat(),
                                    "ERROR",
                                    f"Entry data unavailable for {symbol} {timeframe}: "
                                    f"{type(error).__name__}",
                                )
                            )
                            self._store.set_meta(alert_key, "1")
                        continue
                    self._store.set_meta(
                        f"entry_data_error_alerted:{symbol}:{timeframe}", "0"
                    )
                    window = closed[-1_000:]
                    newest = window[-1]
                    progress = self._store.get_bar_progress(symbol, timeframe)
                    if progress is not None and newest.time.isoformat() <= progress:
                        continue

                    context = self._context_factory(symbol, timeframe, window)
                    context = self._with_server_schedule(context, now)
                    context = replace(
                        context,
                        open_risks=_include_lane_risks(
                            context.open_risks, accepted_risks
                        ),
                        pending_risks=_include_lane_risks(
                            context.pending_risks, pending_risks
                        ),
                        news_events=(
                            self._news_events
                            if self._news_calendar_valid is not None
                            else context.news_events
                        ),
                        news_calendar_valid=(
                            self._news_calendar_valid
                            if self._news_calendar_valid is not None
                            else context.news_calendar_valid
                        ),
                        strategy_mode=self._strategy_mode,
                        bars_since_stop=self._bars_since_stop(
                            symbol, timeframe, window
                        ),
                        stop_new=(
                            context.stop_new
                            or self._store.get_flag("stop_new")
                            or self._store.get_flag("close_all_pending")
                        ),
                        halted_daily=(
                            context.halted_daily or self._store.get_flag("halted_daily")
                        ),
                        halted_monthly=(
                            context.halted_monthly
                            or self._store.get_flag("halted_monthly")
                        ),
                        halted_error=(
                            context.halted_error
                            or self._store.get_flag("halted_error")
                            or self._gateway.degraded
                            or self._store.get_meta("gateway_degraded") == "1"
                        ),
                    )
                    decision = evaluate_candle(context)
                    if decision.reason is ReasonCode.ENTRY_ALLOWED:
                        assert decision.proposal is not None
                        context = self._with_candidate_margin(
                            context,
                            decision.proposal,
                            symbol_settings.commission_per_lot_side,
                        )
                        decision = evaluate_candle(context)
                    if decision.reason is ReasonCode.ENTRY_ALLOWED:
                        assert decision.proposal is not None
                        sized = _size(
                            context,
                            decision.proposal,
                            symbol_settings.commission_per_lot_side,
                        )
                        if sized.lots is None or sized.actual_risk is None:
                            decision = _decision(
                                context,
                                now,
                                _reason(sized.reason),
                                strategy=decision.strategy,
                                proposal=decision.proposal,
                                indicators=decision.indicators,
                            )
                        else:
                            result = self._executor.open_trade(
                                intent_key=(
                                    f"{symbol}-{timeframe}-{newest.time.isoformat()}"
                                ),
                                symbol=symbol,
                                side=decision.proposal.side,
                                lots=sized.lots,
                                entry=decision.proposal.entry,
                                stop_loss=decision.proposal.stop_loss,
                                take_profit=decision.proposal.take_profit,
                                actual_risk=sized.actual_risk,
                                strategy=decision.strategy or "A",
                                timeframe=timeframe,
                            )
                            if result.trade is None:
                                outcome_unknown = result.intent.status in (
                                    "PENDING",
                                    "SENT",
                                    "UNKNOWN",
                                )
                                if outcome_unknown:
                                    pending_risks.append(
                                        OpenRisk(
                                            symbol,
                                            decision.proposal.side,
                                            sized.actual_risk / context.equity * 100.0,
                                        )
                                    )
                                if outcome_unknown:
                                    failure_reason = ReasonCode.UNKNOWN_OUTCOME
                                elif result.reason == ReasonCode.STOP_NEW.value:
                                    failure_reason = ReasonCode.STOP_NEW
                                elif result.reason == ReasonCode.CHECK_FAILED.value:
                                    failure_reason = ReasonCode.CHECK_FAILED
                                else:
                                    failure_reason = ReasonCode.SEND_FAILED
                                decision = _decision(
                                    context,
                                    now,
                                    failure_reason,
                                    strategy=decision.strategy,
                                    proposal=decision.proposal,
                                    indicators=decision.indicators,
                                )
                            else:
                                accepted_risks.append(
                                    OpenRisk(
                                        symbol,
                                        decision.proposal.side,
                                        sized.actual_risk / context.equity * 100.0,
                                    )
                                )

                    record = DecisionRecord(
                        time_utc=decision.time.isoformat(),
                        symbol=decision.symbol,
                        timeframe=timeframe,
                        reason_code=decision.reason.value,
                        strategy=decision.strategy or "",
                        code_version=self._code_version,
                        settings_version=self._settings_version,
                    )
                    committed = self._store.commit_candle_decisions(
                        symbol=symbol,
                        timeframe=timeframe,
                        bar_open_utc=newest.time.isoformat(),
                        decisions=[record],
                    )
                    if committed:
                        decisions.append(decision)
        except Exception as error:
            self._store.set_flag("halted_error", True)
            self._store.enqueue_alert(
                AlertPayload(
                    self._clock.now_utc().astimezone(UTC).isoformat(),
                    "CRITICAL",
                    f"Entry lane failed: {type(error).__name__}",
                )
            )
            raise
        return tuple(decisions)

    def _with_server_schedule(
        self, context: EntryContext, now_utc: datetime
    ) -> EntryContext:
        server_offset = self._gateway.server_utc_offset_hours()
        current_rollover = trading_day_start_utc(now_utc, server_offset)
        if now_utc - current_rollover <= timedelta(
            minutes=self._settings.timing.rollover_block_after_min
        ):
            rollover_at = current_rollover
        else:
            rollover_at = next_trading_day_boundary_utc(now_utc, server_offset)
        weekend_close = next_weekend_close_utc(
            now_utc,
            parse_sessions(self._settings.symbols[context.symbol].hours_utc),
        )
        return replace(
            context,
            now_utc=now_utc,
            rollover_at_utc=rollover_at,
            weekend_close_utc=weekend_close,
        )

    def _build_entry_context(
        self, symbol: str, timeframe: str, candles: tuple[Candle, ...]
    ) -> EntryContext:
        now = _as_utc(self._clock.now_utc())
        account = self._gateway.account()
        positions = self._gateway.positions(self._settings.account.magic_number)
        pending_intents = tuple(
            intent
            for intent in self._store.unresolved_intents()
            if intent.intent_key.startswith("trade|")
        )
        symbol_names = tuple(
            dict.fromkeys(
                (
                    *(
                        name
                        for name, item in self._settings.symbols.items()
                        if item.enabled
                    ),
                    *(position.symbol for position in positions),
                    *(intent.symbol for intent in pending_intents),
                )
            )
        )
        specs = {
            name: self._gateway.symbol_info(name)
            for name in symbol_names
        }
        spec = specs.get(symbol)
        if spec is None:
            raise ValueError(f"No broker specification is available for {symbol}")
        tick = self._gateway.tick(symbol)
        data_error = account.equity <= 0 or tick is None
        open_trades = {trade.ticket: trade for trade in self._store.open_trades()}
        open_risks: list[OpenRisk] = []
        margin_used = 0.0
        margin_calculation_failed = False

        for position in positions:
            trade = open_trades.get(position.ticket)
            if trade is None or position.stop_loss <= 0:
                data_error = True
                continue
            risk_pct = (
                trade.actual_risk / account.equity * 100
                if account.equity > 0
                else 0.0
            )
            open_risks.append(OpenRisk(position.symbol, position.side, risk_pct))
            margin = self._gateway.calculate_margin(
                OrderRequest(
                    symbol=position.symbol,
                    side=position.side,
                    lots=position.lots,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    magic_number=self._settings.account.magic_number,
                    intent_id=f"margin-open-{position.ticket}",
                )
            )
            if margin is None or not isfinite(margin) or margin < 0:
                margin_calculation_failed = True
            else:
                margin_used += margin

        pending_risks: list[OpenRisk] = []
        position_comments = {position.comment for position in positions}
        for intent in pending_intents:
            if str(intent.intent_id) in position_comments:
                continue
            side = Side(intent.side)
            risk_pct = (
                intent.actual_risk / account.equity * 100
                if account.equity > 0
                else 0.0
            )
            pending_risks.append(OpenRisk(intent.symbol, side, risk_pct))
            margin = self._gateway.calculate_margin(
                OrderRequest(
                    symbol=intent.symbol,
                    side=side,
                    lots=intent.lots,
                    stop_loss=intent.stop_loss,
                    take_profit=intent.take_profit,
                    magic_number=self._settings.account.magic_number,
                    intent_id=f"margin-pending-{intent.intent_id}",
                )
            )
            if margin is None or not isfinite(margin) or margin < 0:
                margin_calculation_failed = True
            else:
                margin_used += margin

        day_baseline = self._store.get_baseline("day")
        if day_baseline is None or day_baseline[1] <= 0:
            data_error = True
            start_of_day_equity = 0.0
            realized_pnl_today = 0.0
        else:
            day_start, start_of_day_equity = day_baseline
            realized_pnl_today = self._store.realized_pnl_between(
                day_start, now.isoformat()
            )

        return EntryContext(
            settings=self._settings,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            now_utc=now,
            spec=spec,
            tick=tick,
            specs=specs,
            open_risks=tuple(open_risks),
            pending_risks=tuple(pending_risks),
            margin_used_after_pct=(
                margin_used / account.equity * 100 if account.equity > 0 else 0.0
            ),
            realized_pnl_today=realized_pnl_today,
            start_of_day_equity=start_of_day_equity,
            equity=account.equity,
            stop_new=(
                self._store.get_flag("stop_new")
                or self._store.get_flag("close_all_pending")
            ),
            halted_daily=self._store.get_flag("halted_daily"),
            halted_monthly=self._store.get_flag("halted_monthly"),
            halted_error=(
                self._store.get_flag("halted_error")
                or self._gateway.degraded
                or self._store.get_meta("gateway_degraded") == "1"
            ),
            margin_calculation_failed=margin_calculation_failed,
            data_error=data_error,
        )

    def _with_candidate_margin(
        self,
        context: EntryContext,
        proposal: Proposal,
        commission_per_lot_side: float,
    ) -> EntryContext:
        sized = _size(context, proposal, commission_per_lot_side)
        if sized.lots is None:
            return context
        margin = self._gateway.calculate_margin(
            OrderRequest(
                symbol=context.symbol,
                side=proposal.side,
                lots=sized.lots,
                stop_loss=proposal.stop_loss,
                take_profit=proposal.take_profit,
                magic_number=self._settings.account.magic_number,
                intent_id="margin-entry-preview",
            )
        )
        if margin is None or not isfinite(margin) or margin < 0:
            return replace(context, margin_calculation_failed=True)
        return replace(
            context,
            margin_used_after_pct=(
                context.margin_used_after_pct + margin / context.equity * 100
            ),
        )

    def _bars_since_stop(
        self,
        symbol: str,
        timeframe: str,
        candles: tuple[Candle, ...],
    ) -> int | None:
        last_stop = self._store.last_stop_out_utc(symbol, timeframe)
        if last_stop is None:
            return None
        stop_time = datetime.fromisoformat(last_stop)
        return sum(candle.time > stop_time for candle in candles)

    def start(self) -> ManagementReport:
        self._acquire_instance_lock()
        try:
            if not self._gateway.connect():
                raise ConnectionError("Broker connection failed")
            account = self._gateway.account()
            terminal = self._gateway.terminal_info()
            failures = startup_guard(
                self._settings,
                account,
                terminal_connected=terminal.connected,
                terminal_trade_allowed=terminal.trade_allowed,
            )
            if failures:
                raise RuntimeError("; ".join(failures))
            self._initialize_news_calendar()
            self._store.set_meta("settings_version", self._settings_version)
            for flag in (
                "stop_new",
                "close_all_pending",
                "halted_daily",
                "halted_monthly",
                "halted_error",
            ):
                self._store.get_flag(flag)
            positions = self._gateway.positions(self._settings.account.magic_number)
            for intent in self._store.unresolved_intents():
                self._executor.resolve_intent(intent)
            self._executor.reconcile()
            positions = self._gateway.positions(self._settings.account.magic_number)
            if any(position.stop_loss <= 0 for position in positions):
                raise RuntimeError("A broker position has no stop-loss after reconciliation")
            baseline_account = self._gateway.account()
            now = _as_utc(self._clock.now_utc())
            offset = self._gateway.server_utc_offset_hours()
            expected_baselines = {
                "day": trading_day_start_utc(now, offset),
                "month": month_start_utc(now, offset),
            }
            baseline_late_periods = tuple(
                period
                for period, boundary in expected_baselines.items()
                if (
                    (baseline := self._store.get_baseline(period)) is None
                    or datetime.fromisoformat(baseline[0]) < boundary
                )
            )
            self._initialize_baselines(baseline_account.equity)
            if (
                self._store.get_baseline("day") is None
                or self._store.get_baseline("month") is None
            ):
                raise RuntimeError("Startup could not establish risk baselines")
            if baseline_late_periods:
                self._store.set_meta("baseline_late_utc", now.isoformat())
                self._store.set_meta(
                    "baseline_late_periods", ",".join(baseline_late_periods)
                )
                self._store.enqueue_alert(
                    AlertPayload(
                        now.isoformat(),
                        "WARNING",
                        "baseline_late: risk baselines were initialized late; "
                        "limits are measured from current equity",
                    )
                )
            return self.run_management_lane()
        except Exception as error:
            self.stop()
            self._store.set_flag("halted_error", True)
            self._critical_alert(f"Engine startup failed: {type(error).__name__}")
            raise

    def _initialize_news_calendar(self) -> None:
        if not self._news_enabled:
            self._news_events = ()
            self._news_calendar_valid = True
            self._store.set_meta("news_calendar_mode", "disabled")
            self._store.set_meta("news_calendar_valid", "1")
            return

        self._store.set_meta("news_calendar_mode", "enabled")
        path = self._settings.news.calendar_file
        now = _as_utc(self._clock.now_utc())
        valid = calendar_has_minimum_coverage(
            path,
            now_utc=now,
            min_days_covered=self._settings.news.min_days_covered,
        )
        if not valid:
            self._news_events = ()
            self._news_calendar_valid = False
            self._store.set_meta("news_calendar_valid", "0")
            if self._store.get_meta("news_calendar_invalid_alerted") != "1":
                self._critical_alert(
                    "News calendar is missing, invalid, or lacks minimum future coverage"
                )
                self._store.set_meta("news_calendar_invalid_alerted", "1")
            return

        try:
            events = load_news_calendar(path)
        except (OSError, ValueError):
            self._news_events = ()
            self._news_calendar_valid = False
            self._store.set_meta("news_calendar_valid", "0")
            if self._store.get_meta("news_calendar_invalid_alerted") != "1":
                self._critical_alert("News calendar could not be loaded")
                self._store.set_meta("news_calendar_invalid_alerted", "1")
            return

        self._news_events = tuple(events)
        self._news_calendar_valid = True
        self._store.set_meta("news_calendar_valid", "1")
        self._store.set_meta("news_calendar_invalid_alerted", "0")
        future_events = [event for event in events if event.time_utc >= now]
        coverage = future_events[-1].time_utc - now
        if coverage < timedelta(days=10):
            if self._store.get_meta("news_calendar_low_coverage_alerted") != "1":
                self._store.enqueue_alert(
                    AlertPayload(
                        now.isoformat(),
                        "WARNING",
                        "News calendar coverage is under 10 days",
                    )
                )
                self._store.set_meta("news_calendar_low_coverage_alerted", "1")
        else:
            self._store.set_meta("news_calendar_low_coverage_alerted", "0")

    def stop(self) -> None:
        """Release the process lock without changing broker-held positions."""
        handle = self._instance_lock_file
        if handle is None:
            return
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
            self._instance_lock_file = None

    def _acquire_instance_lock(self) -> None:
        if self._instance_lock_file is not None:
            return
        path = self._store.path.with_suffix(self._store.path.suffix + ".lock")
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise RuntimeError("Another bot instance already holds the state lock") from error
        self._instance_lock_file = handle

    def run_management_lane(self) -> ManagementReport:
        now = _as_utc(self._clock.now_utc())
        self._store.set_meta("heartbeat_utc", now.isoformat())
        try:
            account = self._gateway.account()
            self._gateway.positions(self._settings.account.magic_number)
            for symbol, symbol_settings in self._settings.symbols.items():
                if symbol_settings.enabled and self._gateway.tick(symbol) is None:
                    raise ConnectionError(f"No current tick for {symbol}")
        except (ConnectionError, TimeoutError):
            self._gateway.mark_degraded()
            self._store.set_meta("gateway_degraded", "1")
            if self._store.get_meta("gateway_degraded_alerted") != "1":
                self._store.enqueue_alert(
                    AlertPayload(now.isoformat(), "ERROR", "Broker gateway is degraded")
                )
                self._store.set_meta("gateway_degraded_alerted", "1")
            return ManagementReport(("gateway-degraded",), degraded=True)

        self._store.set_meta("gateway_degraded", "0")
        self._store.set_meta("gateway_degraded_alerted", "0")
        try:
            actions = list(self._executor.reconcile().actions)
            offset = self._gateway.server_utc_offset_hours()
            day_start = trading_day_start_utc(now, offset)
            month_start = month_start_utc(now, offset)
            self._roll_baselines(day_start, month_start, account.equity)
            day_baseline = self._store.get_baseline("day")
            month_baseline = self._store.get_baseline("month")
            if day_baseline is None or month_baseline is None:
                raise RuntimeError("Loss-limit baselines are unavailable")
            loss = evaluate_loss_limits(
                start_of_day_equity=day_baseline[1],
                start_of_month_equity=month_baseline[1],
                current_equity=account.equity,
                limits=RiskLimits(
                    daily_loss_pct=self._settings.risk.daily_loss_pct,
                    monthly_loss_pct=self._settings.risk.monthly_loss_pct,
                    max_net_currency_exposure_pct=(
                        self._settings.risk.max_net_currency_exposure_pct
                    ),
                    max_open_trades=self._settings.risk.max_open_trades,
                    max_margin_used_pct=self._settings.risk.max_margin_used_pct,
                    cooldown_bars_after_stop=(
                        self._settings.risk.cooldown_bars_after_stop
                    ),
                ),
            )
            self._store.set_flag("halted_daily", loss.halted_daily)
            self._store.set_flag("halted_monthly", loss.halted_monthly)
            if loss.halted_daily:
                actions.append("daily-halt")
            if loss.halted_monthly:
                actions.append("monthly-halt")
                for position in self._gateway.positions(
                    self._settings.account.magic_number
                ):
                    if self._executor.close_position(
                        position.ticket, ExitReason.MONTHLY_CAP
                    ):
                        actions.append(f"monthly-close:{position.ticket}")

            if self._store.get_flag("close_all_pending"):
                for position in self._gateway.positions(
                    self._settings.account.magic_number
                ):
                    if self._executor.close_position(position.ticket, ExitReason.CLOSE_ALL):
                        actions.append(f"close-all:{position.ticket}")
                if not self._gateway.positions(self._settings.account.magic_number):
                    self._store.set_flag("close_all_pending", False)

            positions = self._gateway.positions(self._settings.account.magic_number)
            trades = {trade.ticket: trade for trade in self._store.open_trades()}
            for symbol, symbol_settings in self._settings.symbols.items():
                if not symbol_settings.enabled:
                    continue
                close_at = next_weekend_close_utc(
                    now, parse_sessions(symbol_settings.hours_utc)
                )
                if close_at - now <= timedelta(
                    minutes=self._settings.timing.flatten_before_weekend_close_min
                ):
                    for position in positions:
                        if position.symbol == symbol and self._executor.close_position(
                            position.ticket, ExitReason.WEEKEND_FLATTEN
                        ):
                            actions.append(f"weekend-close:{position.ticket}")

            for trade in trades.values():
                if trade.strategy != "B" or trade.timeframe != "H1":
                    continue
                candles = self._gateway.candles(trade.symbol, "H1")
                closed = last_closed_candles(candles, "H1", now_utc=now)
                opened_at = datetime.fromisoformat(trade.opened_at_utc)
                if (
                    sum(candle.time > opened_at for candle in closed)
                    >= self._settings.strategy_b.time_stop_candles
                    and self._executor.close_position(trade.ticket, ExitReason.TIME_STOP)
                ):
                    actions.append(f"time-stop:{trade.ticket}")

            refreshed = self._gateway.account()
            minute = now.replace(second=0, microsecond=0).isoformat()
            self._store.record_equity(minute, refreshed.balance, refreshed.equity)
            return ManagementReport(tuple(actions))
        except Exception as error:
            self._store.set_flag("halted_error", True)
            self._critical_alert(f"Management lane failed: {type(error).__name__}")
            raise

    def _initialize_baselines(self, equity: float) -> None:
        now = _as_utc(self._clock.now_utc())
        offset = self._gateway.server_utc_offset_hours()
        self._roll_baselines(
            trading_day_start_utc(now, offset),
            month_start_utc(now, offset),
            equity,
        )

    def _roll_baselines(
        self, day_start: datetime, month_start: datetime, equity: float
    ) -> None:
        current_day = self._store.get_baseline("day")
        current_month = self._store.get_baseline("month")
        if current_day is None or current_day[0] < day_start.isoformat():
            if current_day is not None:
                self._store.set_flag("halted_daily", False)
            self._store.new_baseline("day", day_start.isoformat(), equity)
        if current_month is None or current_month[0] < month_start.isoformat():
            if current_month is not None:
                self._store.set_flag("halted_monthly", False)
            self._store.new_baseline("month", month_start.isoformat(), equity)

    def _critical_alert(self, message: str) -> None:
        self._store.enqueue_alert(
            AlertPayload(
                _as_utc(self._clock.now_utc()).isoformat(),
                "CRITICAL",
                message,
            )
        )
