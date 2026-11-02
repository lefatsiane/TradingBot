from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite

from bot.core.types import OpenRisk, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class RiskLimits:
    daily_loss_pct: float = 1.0
    monthly_loss_pct: float = 3.0
    max_net_currency_exposure_pct: float = 1.0
    max_open_trades: int = 3
    max_margin_used_pct: float = 30.0
    cooldown_bars_after_stop: int = 6

    def __post_init__(self) -> None:
        numeric_limits = (
            self.daily_loss_pct,
            self.monthly_loss_pct,
            self.max_net_currency_exposure_pct,
            self.max_margin_used_pct,
        )
        if not all(isfinite(value) for value in numeric_limits):
            raise ValueError("Risk limits must be finite")
        if (
            min(
                self.daily_loss_pct,
                self.monthly_loss_pct,
                self.max_net_currency_exposure_pct,
            )
            <= 0
        ):
            raise ValueError("Risk limits must be positive")
        if self.max_open_trades < 1:
            raise ValueError("max_open_trades must be at least 1")
        if self.max_margin_used_pct <= 0 or self.cooldown_bars_after_stop < 0:
            raise ValueError("Margin limit must be positive and cooldown cannot be negative")


@dataclass(frozen=True, slots=True)
class LossLimitStatus:
    daily_loss_pct: float
    monthly_loss_pct: float
    halted_daily: bool
    halted_monthly: bool


def evaluate_loss_limits(
    *,
    start_of_day_equity: float,
    start_of_month_equity: float,
    current_equity: float,
    limits: RiskLimits | None = None,
) -> LossLimitStatus:
    """Evaluate daily and monthly loss limits using current equity, including floating P&L."""
    if limits is None:
        limits = RiskLimits()
    equity_values = (start_of_day_equity, start_of_month_equity, current_equity)
    if not all(isfinite(value) and value > 0 for value in equity_values):
        raise ValueError("Equity values must be positive")
    daily_loss_pct = max(
        0.0,
        (start_of_day_equity - current_equity) / start_of_day_equity * 100.0,
    )
    monthly_loss_pct = max(
        0.0,
        (start_of_month_equity - current_equity) / start_of_month_equity * 100.0,
    )
    return LossLimitStatus(
        daily_loss_pct=daily_loss_pct,
        monthly_loss_pct=monthly_loss_pct,
        halted_daily=daily_loss_pct >= limits.daily_loss_pct,
        halted_monthly=monthly_loss_pct >= limits.monthly_loss_pct,
    )


@dataclass(frozen=True, slots=True)
class EntryRiskResult:
    allowed: bool
    reason: str | None
    currency_exposure_pct: tuple[tuple[str, float], ...]
    daily_headroom_pct: float


def currency_exposure(
    positions: Iterable[OpenRisk],
    specs: dict[str, SymbolSpec],
) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for position in positions:
        try:
            spec = specs[position.symbol]
        except KeyError as error:
            raise ValueError(f"Missing symbol specification: {position.symbol}") from error
        direction = 1.0 if position.side is Side.LONG else -1.0
        exposure[spec.base_currency] = (
            exposure.get(spec.base_currency, 0.0) + direction * position.risk_pct
        )
        exposure[spec.quote_currency] = (
            exposure.get(spec.quote_currency, 0.0) - direction * position.risk_pct
        )
    return exposure


def evaluate_entry(
    *,
    symbol: str,
    side: Side,
    new_risk_pct: float,
    positions: Iterable[OpenRisk],
    pending: Iterable[OpenRisk],
    specs: dict[str, SymbolSpec],
    realized_pnl_today: float,
    start_of_day_equity: float,
    margin_used_after_pct: float,
    bars_since_stop: int | None,
    limits: RiskLimits | None = None,
) -> EntryRiskResult:
    if limits is None:
        limits = RiskLimits()
    if not all(
        isfinite(value)
        for value in (
            start_of_day_equity,
            new_risk_pct,
            realized_pnl_today,
            margin_used_after_pct,
        )
    ):
        raise ValueError("Risk inputs must be finite")
    if start_of_day_equity <= 0 or new_risk_pct <= 0:
        raise ValueError("start-of-day equity and new risk must be positive")
    if margin_used_after_pct < 0:
        raise ValueError("margin_used_after_pct cannot be negative")
    if bars_since_stop is not None and bars_since_stop < 0:
        raise ValueError("bars_since_stop cannot be negative")
    current = list(positions)
    inflight = list(pending)
    all_risks = current + inflight
    headroom = max(
        0.0,
        limits.daily_loss_pct
        + realized_pnl_today / start_of_day_equity * 100.0,
    )
    headroom = max(0.0, headroom - sum(position.risk_pct for position in all_risks))
    if new_risk_pct > headroom + 1e-12:
        return EntryRiskResult(False, "DAILY_HEADROOM", (), headroom)
    if len(all_risks) >= limits.max_open_trades:
        return EntryRiskResult(False, "MAX_TRADES", (), headroom)
    if any(position.symbol == symbol for position in all_risks):
        return EntryRiskResult(False, "SLOT_TAKEN", (), headroom)
    if (
        bars_since_stop is not None
        and bars_since_stop < limits.cooldown_bars_after_stop
    ):
        return EntryRiskResult(False, "COOLDOWN", (), headroom)
    if margin_used_after_pct > limits.max_margin_used_pct:
        return EntryRiskResult(False, "NO_MARGIN", (), headroom)

    spec = specs.get(symbol)
    if spec is None:
        raise ValueError(f"Missing symbol specification: {symbol}")
    exposure = currency_exposure(all_risks, specs)
    direction = 1.0 if side is Side.LONG else -1.0
    exposure[spec.base_currency] = exposure.get(spec.base_currency, 0.0) + direction * new_risk_pct
    exposure[spec.quote_currency] = (
        exposure.get(spec.quote_currency, 0.0) - direction * new_risk_pct
    )
    if any(
        abs(value) > limits.max_net_currency_exposure_pct + 1e-12 for value in exposure.values()
    ):
        return EntryRiskResult(
            False, "CURRENCY_EXPOSURE", tuple(sorted(exposure.items())), headroom
        )
    return EntryRiskResult(True, None, tuple(sorted(exposure.items())), headroom)
