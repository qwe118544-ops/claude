"""Order book model and level-by-level VWAP fill walking.

A book is two sorted ladders of (price, size) levels, with size in *shares*.
Buys consume the ask ladder from the best (lowest) ask up; sells consume the
bid ladder from the best (highest) bid down. Notional at a level = price*size.

This is where mid-price fantasies die: a 25 USDC order can sweep several ticks
in a thin weather market, and the VWAP you actually pay is the honest cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Level:
    price: float   # probability in (0, 1)
    size: float    # shares available at this price


@dataclass
class OrderBook:
    token_id: str
    bids: list      # descending price; people willing to BUY yes from us
    asks: list      # ascending price; people willing to SELL yes to us
    ts: float = 0.0

    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def mid(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return b if a is None else a
        return (b + a) / 2.0

    def sorted(self) -> "OrderBook":
        return OrderBook(
            self.token_id,
            sorted(self.bids, key=lambda x: -x.price),
            sorted(self.asks, key=lambda x: x.price),
            self.ts,
        )


@dataclass
class FillWalk:
    filled_shares: float
    filled_notional: float
    avg_price: float
    fully_filled: bool


def fill_buy(book: OrderBook, budget_usdc: float, limit_price: Optional[float] = None) -> FillWalk:
    """Spend up to `budget_usdc` walking the ask ladder; respect a limit price."""
    remaining = budget_usdc
    shares = 0.0
    spent = 0.0
    for lvl in sorted(book.asks, key=lambda x: x.price):
        if limit_price is not None and lvl.price > limit_price + 1e-12:
            break
        level_notional = lvl.price * lvl.size
        take = min(remaining, level_notional)
        if take <= 0:
            break
        shares += take / lvl.price
        spent += take
        remaining -= take
        if remaining <= 1e-12:
            break
    avg = spent / shares if shares > 0 else 0.0
    fully = (budget_usdc - spent) <= 1e-9
    return FillWalk(shares, spent, avg, fully)


def fill_sell(book: OrderBook, shares_to_sell: float, limit_price: Optional[float] = None) -> FillWalk:
    """Sell `shares_to_sell` walking the bid ladder; respect a floor limit price."""
    remaining = shares_to_sell
    shares = 0.0
    proceeds = 0.0
    for lvl in sorted(book.bids, key=lambda x: -x.price):
        if limit_price is not None and lvl.price < limit_price - 1e-12:
            break
        take = min(remaining, lvl.size)
        if take <= 0:
            break
        shares += take
        proceeds += take * lvl.price
        remaining -= take
        if remaining <= 1e-12:
            break
    avg = proceeds / shares if shares > 0 else 0.0
    fully = remaining <= 1e-9
    return FillWalk(shares, proceeds, avg, fully)
