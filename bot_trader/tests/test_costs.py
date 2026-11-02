from __future__ import annotations

import pytest

from bot.core.costs import estimate_costs
from bot.core.sizing import size_trade
from bot.core.types import Proposal, Side, SymbolSpec

SPEC = SymbolSpec(
    "EURUSD",
    "EUR",
    "USD",
    point=0.00001,
    tick_size=0.0001,
    tick_value_loss=10.0,
    volume_min=0.01,
    volume_step=0.01,
    volume_max=100.0,
)
PROPOSAL = Proposal(Side.LONG, 1.1, 1.099, 1.102, "TEST")


def test_cost_estimate_includes_spread_commission_and_both_slippage_sides() -> None:
    estimate = estimate_costs(
        PROPOSAL,
        SPEC,
        spread_price=0.00001,
        commission_per_lot_side=3.5,
        slippage_price=0.00001,
    )

    assert estimate.loss_per_lot == pytest.approx(108.0)
    assert estimate.cost_per_lot == pytest.approx(10.0)
    assert estimate.cost_pct_of_risk == pytest.approx(100 * 10 / 108)


def test_size_trade_skips_when_cost_exceeds_risk_budget() -> None:
    result = size_trade(
        3_000,
        0.5,
        PROPOSAL,
        SPEC,
        bid_price=PROPOSAL.entry,
        ask_price=PROPOSAL.entry + 0.001,
        commission_per_lot_side=3.5,
        slippage_price=0.0001,
        cost_gate_pct_of_risk=15,
    )

    assert result.lots is None
    assert result.reason == "COST_TOO_HIGH"


def test_tiny_equity_skips_below_broker_minimum_volume() -> None:
    result = size_trade(
        1.0,
        0.5,
        PROPOSAL,
        SPEC,
        bid_price=PROPOSAL.entry,
        ask_price=PROPOSAL.entry,
        commission_per_lot_side=0,
        slippage_price=0,
    )

    assert result.lots is None
    assert result.reason == "BELOW_MIN_LOT"


@pytest.mark.parametrize(
    "proposal",
    [
        Proposal(Side.LONG, 1.1, 1.099, 1.102, "LONG"),
        Proposal(Side.SHORT, 1.1, 1.101, 1.098, "SHORT"),
    ],
)
def test_sizing_uses_ask_for_longs_and_bid_for_shorts(proposal: Proposal) -> None:
    result = size_trade(
        1_000,
        0.5,
        proposal,
        SPEC,
        bid_price=1.1,
        ask_price=1.1001,
        commission_per_lot_side=0,
        slippage_price=0.00001,
        cost_gate_pct_of_risk=100,
    )

    assert result.lots is not None
    assert result.costs.loss_per_lot == pytest.approx(112)


@pytest.mark.parametrize(
    "value",
    [-0.1, float("nan"), float("inf")],
)
def test_cost_estimate_rejects_negative_or_non_finite_inputs(value: float) -> None:
    with pytest.raises(ValueError):
        estimate_costs(
            PROPOSAL,
            SPEC,
            spread_price=value,
            commission_per_lot_side=0,
            slippage_price=0,
        )
