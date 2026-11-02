from __future__ import annotations

import math
from dataclasses import dataclass
from math import isfinite

from bot.core.costs import CostEstimate, estimate_costs
from bot.core.types import Proposal, Side, SymbolSpec


@dataclass(frozen=True, slots=True)
class SizeResult:
    lots: float | None
    actual_risk: float | None
    reason: str | None
    costs: CostEstimate


def size_trade(
    equity: float,
    risk_pct: float,
    proposal: Proposal,
    spec: SymbolSpec,
    *,
    bid_price: float,
    ask_price: float,
    commission_per_lot_side: float,
    slippage_price: float,
    cost_gate_pct_of_risk: float = 15.0,
) -> SizeResult:
    if not isfinite(equity) or not isfinite(risk_pct) or equity <= 0 or not 0 < risk_pct <= 100:
        raise ValueError("equity must be positive and risk_pct must be in (0, 100]")
    if not isfinite(cost_gate_pct_of_risk) or cost_gate_pct_of_risk < 0:
        raise ValueError("cost gate must be finite and non-negative")
    if (
        not isfinite(bid_price)
        or not isfinite(ask_price)
        or bid_price <= 0
        or ask_price < bid_price
    ):
        raise ValueError("Market bid and ask must be finite, positive, and ordered")
    spread_price = ask_price - bid_price
    entry_price = (
        ask_price + slippage_price
        if proposal.side is Side.LONG
        else bid_price - slippage_price
    )
    fill_proposal = Proposal(
        side=proposal.side,
        entry=entry_price,
        stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit,
        reason=proposal.reason,
        indicators=proposal.indicators,
    )
    costs = estimate_costs(
        fill_proposal,
        spec,
        spread_price=spread_price,
        commission_per_lot_side=commission_per_lot_side,
        slippage_price=slippage_price,
    )
    if costs.cost_pct_of_risk > cost_gate_pct_of_risk:
        return SizeResult(None, None, "COST_TOO_HIGH", costs)

    risk_money = equity * risk_pct / 100.0
    if not isfinite(risk_money):
        raise ValueError("Requested risk exceeds finite numeric limits")
    raw_steps = risk_money / costs.loss_per_lot / spec.volume_step
    if not isfinite(raw_steps):
        raise ValueError("Calculated volume exceeds finite numeric limits")
    steps = math.floor(raw_steps + 1e-12)
    lots = min(steps * spec.volume_step, spec.volume_max)
    lots = round(lots, 8)
    if lots + 1e-10 < spec.volume_min:
        return SizeResult(None, None, "BELOW_MIN_LOT", costs)
    actual_risk = lots * costs.loss_per_lot
    if actual_risk > risk_money + max(1e-8, risk_money * 1e-10):
        raise ArithmeticError("Rounded trade risk exceeds the requested risk")
    return SizeResult(lots, actual_risk, None, costs)
