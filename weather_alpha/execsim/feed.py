"""Book feed: provides the order book for a token at a given (sim) time.

The key property for a faithful execution sim is that the book *evolves with
time*. When an order arrives 300-1000 ms after the signal, it fills against the
book as it is *then*, not as it was at signal time — so latency produces real,
stochastic slippage instead of a free fill.

`SimulatedBookFeed` drives a fair-price random walk per token and builds a
ladder around it. Depth is deliberately shallow (thin weather markets) so a
20-30 USDC order sweeps a couple of ticks — exactly where naive backtests lie.
"""

from __future__ import annotations

import random
from typing import Optional, Protocol

from .book import Level, OrderBook


class BookFeed(Protocol):
    def book_at(self, token_id: str, t: float) -> OrderBook: ...


class SimulatedBookFeed:
    def __init__(
        self,
        tick: float = 0.01,
        n_levels: int = 6,
        depth_shares: float = 40.0,
        vol_per_s: float = 0.01,      # fair-price stdev per sqrt-second
        horizon_s: float = 5.0,
        dt: float = 0.05,
        seed: int = 0,
    ) -> None:
        self.tick = tick
        self.n_levels = n_levels
        self.depth_shares = depth_shares
        self.vol_per_s = vol_per_s
        self.horizon_s = horizon_s
        self.dt = dt
        self._rng = random.Random(seed)
        self._paths: dict[str, list[float]] = {}
        self._params: dict[str, dict] = {}

    def register(self, token_id: str, fair0: float, drift_per_s: float = 0.0,
                 depth_shares: Optional[float] = None) -> None:
        """Precompute a deterministic fair-price path for reproducible fills."""
        n = int(self.horizon_s / self.dt) + 2
        path = [fair0]
        step_sd = self.vol_per_s * (self.dt ** 0.5)
        for _ in range(n):
            nxt = path[-1] + drift_per_s * self.dt + self._rng.gauss(0, step_sd)
            path.append(min(0.99, max(0.01, nxt)))
        self._paths[token_id] = path
        self._params[token_id] = {"depth": depth_shares or self.depth_shares}

    def _fair(self, token_id: str, t: float) -> float:
        path = self._paths.get(token_id)
        if not path:
            raise KeyError(f"token {token_id} not registered in feed")
        idx = min(len(path) - 1, max(0, int(round(t / self.dt))))
        return path[idx]

    def book_at(self, token_id: str, t: float) -> OrderBook:
        fair = self._fair(token_id, t)
        depth = self._params[token_id]["depth"]
        asks, bids = [], []
        for i in range(self.n_levels):
            ask_p = min(0.999, fair + self.tick * (i + 0.5))
            bid_p = max(0.001, fair - self.tick * (i + 0.5))
            # depth thickens away from the top of book
            size = depth * (1.0 + 0.5 * i)
            asks.append(Level(round(ask_p, 4), round(size, 2)))
            bids.append(Level(round(bid_p, 4), round(size, 2)))
        return OrderBook(token_id, bids, asks, ts=t)
