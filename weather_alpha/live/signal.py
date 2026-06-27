"""The brain: turn (my probability, market price) into a tradeable signal.

A signal is tradeable only when FOUR conditions hold at once — this is the
filter that keeps you from acting on noise:

    1. Edge:        |my_prob - market_prob| >= min_edge
    2. Liquidity:   enough size resting at the price you'd take
    3. Timing:      settlement is within a sensible window (not too far out,
                    where the forecast hasn't converged; not already past)
    4. Confidence:  the contract was parsed confidently (right city/threshold/date)

Side selection:
    my_prob > market_prob + edge  -> BUY YES  (market underprices the event)
    my_prob < market_prob - edge  -> BUY NO   (market overprices the event)

This module is pure logic with no network, so it is fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .markets import WeatherMarket


@dataclass
class SignalParams:
    min_edge: float = 0.08            # require >= 8 percentage points
    min_liquidity_usdc: float = 50.0  # require tradeable size at the price
    max_hours_to_settle: float = 96.0 # ignore markets settling >4 days out
    min_hours_to_settle: float = 0.0  # ignore already-settled markets


@dataclass
class Signal:
    market_id: str
    question: str
    my_prob: float
    market_prob: float
    edge: float                 # my_prob - market_prob (signed)
    side: Optional[str]         # "BUY_YES" | "BUY_NO" | None
    tradeable: bool
    hours_to_settle: Optional[float]
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "my_prob": round(self.my_prob, 4),
            "market_prob": round(self.market_prob, 4),
            "edge": round(self.edge, 4),
            "side": self.side,
            "tradeable": self.tradeable,
            "hours_to_settle": (None if self.hours_to_settle is None
                                else round(self.hours_to_settle, 2)),
            "reasons": self.reasons,
        }


def _price_to_take(market: WeatherMarket, side: str) -> Optional[float]:
    """The price you'd actually pay (taker), preferring the book over the mid."""
    if side == "BUY_YES":
        return market.best_ask if market.best_ask is not None else market.yes_price
    if side == "BUY_NO":
        # Buying NO at price (1 - best_bid_of_YES): you lift the YES bid side.
        if market.best_bid is not None:
            return 1.0 - market.best_bid
        return None if market.yes_price is None else 1.0 - market.yes_price
    return None


def _liquidity_for(market: WeatherMarket, side: str) -> Optional[float]:
    if side == "BUY_YES":
        return market.top_ask_size
    if side == "BUY_NO":
        return market.top_bid_size
    return None


def evaluate_signal(
    market: WeatherMarket,
    my_prob: float,
    params: Optional[SignalParams] = None,
    now=None,
) -> Signal:
    """Produce a Signal, applying the four-condition filter."""
    params = params or SignalParams()
    reasons: list[str] = []

    market_prob = market.yes_price
    if market_prob is None:
        return Signal(market.market_id, market.question, my_prob, float("nan"),
                      float("nan"), None, False, market.hours_to_settle(now),
                      ["no market price available"])

    edge = my_prob - market_prob

    # Direction of conviction.
    if edge >= params.min_edge:
        side = "BUY_YES"
    elif -edge >= params.min_edge:
        side = "BUY_NO"
    else:
        side = None
        reasons.append(f"edge {edge:+.3f} below threshold {params.min_edge:.3f}")

    # Condition 4: parse confidence.
    if not market.is_tradeable_contract():
        reasons.append("contract not parsed confidently; will not trade")

    # Condition 3: timing.
    hts = market.hours_to_settle(now)
    if hts is not None:
        if hts < params.min_hours_to_settle:
            reasons.append(f"already settling/settled (h_to_settle={hts:.1f})")
        elif hts > params.max_hours_to_settle:
            reasons.append(
                f"settles too far out ({hts:.1f}h > {params.max_hours_to_settle:.0f}h); "
                "forecast not yet trustworthy")

    # Condition 2: liquidity (only meaningful if we have a side).
    if side is not None:
        liq = _liquidity_for(market, side)
        if liq is None:
            reasons.append("no order-book size known; cannot confirm liquidity")
        elif liq < params.min_liquidity_usdc:
            reasons.append(
                f"thin book: {liq:.0f} USDC < {params.min_liquidity_usdc:.0f} at the price")

    tradeable = side is not None and len(reasons) == 0
    if tradeable:
        take = _price_to_take(market, side)
        reasons.append(
            f"TRADEABLE {side} @ ~{take:.3f} (my {my_prob:.3f} vs mkt {market_prob:.3f}, "
            f"edge {edge:+.3f}, {hts:.1f}h to settle)")

    return Signal(
        market_id=market.market_id,
        question=market.question,
        my_prob=my_prob,
        market_prob=market_prob,
        edge=edge,
        side=side if tradeable else None,
        tradeable=tradeable,
        hours_to_settle=hts,
        reasons=reasons,
    )
