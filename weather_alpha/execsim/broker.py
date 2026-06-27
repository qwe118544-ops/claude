"""PaperBroker: the execution simulator that ties it all together.

submit(order):
    1. sample latency -> order arrives later than the signal;
    2. fetch the book *at arrival time* (it has moved -> real slippage);
    3. walk the ladder for a VWAP fill, honouring the limit price;
    4. apply FOK (all-or-nothing) / FAK (partial then cancel) semantics;
    5. charge the market's dynamic fee;
    6. update the position (entries) or realise P&L (exits).

settle(market_id, token_id, event):
    pay the binary outcome and book settlement P&L.

stats():
    net P&L, gross P&L, fees, average slippage, and unfilled rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .book import OrderBook, fill_buy, fill_sell
from .fees import FeeModel
from .feed import BookFeed
from .latency import LatencyModel
from .orders import Fill, Order, OrderType, Position, Side


@dataclass
class ExecutionStats:
    n_orders: int = 0
    n_filled: int = 0
    n_partial: int = 0
    n_cancelled: int = 0
    requested_notional: float = 0.0
    filled_notional: float = 0.0
    fees: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    _slip_weight: float = 0.0
    _slip_accum: float = 0.0

    @property
    def unfilled_rate(self) -> float:
        if self.requested_notional <= 0:
            return 0.0
        return max(0.0, self.requested_notional - self.filled_notional) / self.requested_notional

    @property
    def avg_slippage_per_share(self) -> float:
        return self._slip_accum / self._slip_weight if self._slip_weight > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "n_orders": self.n_orders,
            "n_filled": self.n_filled,
            "n_partial": self.n_partial,
            "n_cancelled": self.n_cancelled,
            "requested_notional": round(self.requested_notional, 2),
            "filled_notional": round(self.filled_notional, 2),
            "unfilled_rate": round(self.unfilled_rate, 4),
            "avg_slippage_per_share": round(self.avg_slippage_per_share, 5),
            "fees": round(self.fees, 4),
            "gross_pnl": round(self.gross_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
        }


class PaperBroker:
    def __init__(self, feed: BookFeed, fee_model: Optional[FeeModel] = None,
                 latency: Optional[LatencyModel] = None) -> None:
        self.feed = feed
        self.fees = fee_model or FeeModel(default_rate=0.0)
        self.latency = latency or LatencyModel()
        self.positions: dict[tuple, Position] = {}
        self.fill_log: list[Fill] = []
        self.stats = ExecutionStats()

    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Fill:
        self.stats.n_orders += 1
        latency_ms = self.latency.sample_ms()
        arrival = order.signal_time + latency_ms / 1000.0
        book = self.feed.book_at(order.token_id, arrival)

        if order.side == Side.SELL:
            fill = self._fill_exit(order, book, arrival, latency_ms)
        else:
            fill = self._fill_entry(order, book, arrival, latency_ms)

        self.fill_log.append(fill)
        self._account(fill)
        return fill

    # ------------------------------------------------------------------ #
    def _fill_entry(self, order: Order, book: OrderBook, arrival: float,
                    latency_ms: float) -> Fill:
        budget = order.budget_usdc or 0.0
        ref = order.ref_price if order.ref_price is not None else book.best_ask()
        walk = fill_buy(book, budget, order.limit_price)

        if order.order_type == OrderType.FOK and not walk.fully_filled:
            return self._cancelled(order, arrival, latency_ms, budget)

        fee = self.fees.fee(order.market_id, order.token_id, walk.filled_notional)
        status = "FILLED" if walk.fully_filled else ("PARTIAL" if walk.filled_shares > 0 else "CANCELLED")
        slip = (walk.avg_price - ref) if (ref is not None and walk.filled_shares > 0) else 0.0

        if walk.filled_shares > 0:
            key = (order.market_id, order.token_id)
            pos = self.positions.get(key)
            if pos is None:
                pos = Position(order.market_id, order.token_id, order.side,
                               question=getattr(order, "question", ""))
                self.positions[key] = pos
            pos.shares += walk.filled_shares
            pos.cost_basis += walk.filled_notional
            pos.fees_paid += fee
            pos.fills.append(order)

        return Fill(order, arrival, latency_ms, walk.filled_shares, walk.filled_notional,
                    walk.avg_price, fee, budget, budget - walk.filled_notional, slip, status)

    def _fill_exit(self, order: Order, book: OrderBook, arrival: float,
                   latency_ms: float) -> Fill:
        key = (order.market_id, order.token_id)
        pos = self.positions.get(key)
        shares_req = min(order.shares or 0.0, pos.shares if pos else 0.0)
        ref = order.ref_price if order.ref_price is not None else book.best_bid()
        walk = fill_sell(book, shares_req, order.limit_price)

        if order.order_type == OrderType.FOK and not walk.fully_filled:
            return self._cancelled(order, arrival, latency_ms, shares_req * (ref or 0.0))

        fee = self.fees.fee(order.market_id, order.token_id, walk.filled_notional)
        status = "FILLED" if walk.fully_filled else ("PARTIAL" if walk.filled_shares > 0 else "CANCELLED")
        slip = (ref - walk.avg_price) if (ref is not None and walk.filled_shares > 0) else 0.0

        if pos and walk.filled_shares > 0:
            cost_of_sold = pos.avg_price() * walk.filled_shares
            pos.realized_pnl += walk.filled_notional - cost_of_sold
            pos.cost_basis -= cost_of_sold
            pos.shares -= walk.filled_shares
            pos.fees_paid += fee
        req_notional = shares_req * (ref or 0.0)
        return Fill(order, arrival, latency_ms, walk.filled_shares, walk.filled_notional,
                    walk.avg_price, fee, req_notional, req_notional - walk.filled_notional,
                    slip, status)

    def _cancelled(self, order: Order, arrival: float, latency_ms: float,
                   requested_notional: float) -> Fill:
        return Fill(order, arrival, latency_ms, 0.0, 0.0, 0.0, 0.0,
                    requested_notional, requested_notional, 0.0, "CANCELLED")

    # ------------------------------------------------------------------ #
    def settle(self, market_id: str, token_id: str, event: bool) -> float:
        """Resolve a position to the binary outcome; returns settlement P&L."""
        key = (market_id, token_id)
        pos = self.positions.get(key)
        if pos is None or pos.settled:
            return 0.0
        long_yes = pos.side == Side.BUY_YES
        wins = event if long_yes else (not event)
        payoff = 1.0 if wins else 0.0
        settle_value = pos.shares * payoff
        settlement_pnl = settle_value - pos.cost_basis
        pos.realized_pnl += settlement_pnl
        pos.settle_value = settle_value
        pos.cost_basis = 0.0
        pos.shares = 0.0
        pos.settled = True
        self._recompute_pnl()
        return settlement_pnl

    def _recompute_pnl(self) -> None:
        self.stats.gross_pnl = sum(p.realized_pnl for p in self.positions.values())
        self.stats.net_pnl = self.stats.gross_pnl - self.stats.fees

    # ------------------------------------------------------------------ #
    def _account(self, fill: Fill) -> None:
        s = self.stats
        if fill.status == "CANCELLED":
            s.n_cancelled += 1
        elif fill.status == "PARTIAL":
            s.n_partial += 1
        else:
            s.n_filled += 1
        if fill.order.side != Side.SELL:
            s.requested_notional += fill.requested_notional
            s.filled_notional += fill.filled_notional
        s.fees += fill.fee
        if fill.filled_shares > 0:
            s._slip_accum += fill.slippage_per_share * fill.filled_shares
            s._slip_weight += fill.filled_shares
        # realized trading P&L is derived from positions (exits + settlement)
        self._recompute_pnl()

    def open_positions(self) -> list:
        return [p for p in self.positions.values() if not p.settled and p.shares > 1e-9]
