"""Bet sizing, clamped to the (10, 30) USDC band.

Conviction comes from the edge, shaped like a fractional-Kelly stake but then
hard-clamped into the requested band. The clamp dominates on purpose: in thin
weather markets capacity is tiny, so the right behaviour is small, uniform-ish
bets, not pile-ins. Edge below the trade threshold never reaches here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SizingParams:
    min_bet: float = 10.0
    max_bet: float = 30.0
    edge_floor: float = 0.08   # edge at/below which we'd be at the min bet
    edge_cap: float = 0.25     # edge at/above which we hit the max bet
    kelly_fraction: float = 0.25


def _kelly_fraction(my_prob: float, take_price: float) -> float:
    """Fractional-Kelly stake fraction for a binary at `take_price`.

    For a YES bet at price a winning 1.0: net odds b=(1-a)/a, Kelly
    f* = q - (1-q)*a/(1-a). Returned clipped to [0, 1].
    """
    a = min(max(take_price, 1e-4), 1 - 1e-4)
    q = min(max(my_prob, 0.0), 1.0)
    f = q - (1 - q) * a / (1 - a)
    return max(0.0, min(1.0, f))


def size_bet(my_prob: float, take_price: float, edge: float,
             params: SizingParams | None = None) -> float:
    """Return a bet size in USDC, strictly within [min_bet, max_bet]."""
    p = params or SizingParams()
    # Edge-shaped position in the band ...
    span = max(p.edge_cap - p.edge_floor, 1e-9)
    frac = (abs(edge) - p.edge_floor) / span
    frac = max(0.0, min(1.0, frac))
    # ... nudged by fractional Kelly (keeps low-conviction bets near the floor).
    kelly = _kelly_fraction(my_prob, take_price)
    blend = 0.5 * frac + 0.5 * min(1.0, kelly / max(p.kelly_fraction, 1e-9) * p.kelly_fraction * 4)
    blend = max(0.0, min(1.0, blend))
    size = p.min_bet + blend * (p.max_bet - p.min_bet)
    return float(max(p.min_bet, min(p.max_bet, size)))
