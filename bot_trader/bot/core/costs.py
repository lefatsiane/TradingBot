from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from bot.core.types import Proposal, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class CostEstimate:
    loss_per_lot: float
    cost_per_lot: float
    cost_pct_of_risk: float


def estimate_costs(
    proposal: Proposal,
    spec: SymbolSpec,
    *,
    spread_price: float,
    commission_per_lot_side: float,
    slippage_price: float,
) -> CostEstimate:
    values = (
        proposal.entry,
        proposal.stop_loss,
        spec.tick_size,
        spec.tick_value_loss,
        spread_price,
        commission_per_lot_side,
        slippage_price,
    )
    if not all(isfinite(value) for value in values):
        raise ValueError("Prices and cost inputs must be finite")
    if min(spread_price, commission_per_lot_side, slippage_price) < 0:
        raise ValueError("Costs cannot be negative")
    if spec.tick_size <= 0 or spec.tick_value_loss <= 0:
        raise ValueError("Tick size and tick value must be positive")

    stop_distance = abs(proposal.entry - proposal.stop_loss)
    if proposal.side is Side.SHORT:
        stop_distance += spread_price
    if stop_distance <= 0:
        raise ValueError("Stop distance must be positive")
    loss_per_lot = (
        (stop_distance + slippage_price) / spec.tick_size * spec.tick_value_loss
        + 2.0 * commission_per_lot_side
    )
    spread_cost = spread_price / spec.tick_size * spec.tick_value_loss
    cost_per_lot = (
        spread_cost
        + 2.0 * slippage_price / spec.tick_size * spec.tick_value_loss
        + 2.0 * commission_per_lot_side
    )
    if not isfinite(loss_per_lot) or not isfinite(cost_per_lot):
        raise ValueError("Cost calculation exceeded finite numeric limits")
    cost_pct = 100.0 * cost_per_lot / loss_per_lot
    if not isfinite(cost_pct):
        raise ValueError("Cost percentage exceeded finite numeric limits")
    return CostEstimate(loss_per_lot, cost_per_lot, cost_pct)
