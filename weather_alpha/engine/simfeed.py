"""A simulated market universe so the whole stack runs end-to-end offline.

Each market has a hidden true event probability. Our model sees it with small
noise (we are skilled/calibrated); the *market* price sees it with a per-market
bias (some markets are mispriced) plus noise. Where the market bias is large,
an exploitable edge appears. Settlement samples the true Bernoulli outcome.

This stands in for the live Open-Meteo + Polymarket feeds (blocked here) and
lets the engine, sizing, paper broker, and dashboard produce real numbers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

_CITIES = [
    ("new york", 40.78, -73.97), ("chicago", 41.88, -87.63),
    ("los angeles", 34.05, -118.24), ("miami", 25.76, -80.19),
    ("denver", 39.74, -104.99), ("phoenix", 33.45, -112.07),
]


@dataclass
class SimMarket:
    market_id: str
    question: str
    city: str
    latitude: float
    longitude: float
    threshold_f: float
    yes_token_id: str
    no_token_id: str
    settle_dt: datetime
    true_p: float                 # hidden true event probability
    market_bias: float            # systematic market mispricing (signed)
    depth_shares: float
    # dynamic (updated each tick)
    model_prob: float = 0.5       # our probability this tick
    market_yes_fair: float = 0.5  # market mid this tick
    resolved: bool = False
    event_outcome: Optional[bool] = None


class SimUniverse:
    def __init__(self, n_markets: int = 8, seed: int = 42,
                 hours_step: float = 1.0,
                 start: Optional[datetime] = None,
                 model_sigma: float = 0.04, market_sigma: float = 0.03) -> None:
        self.rng = random.Random(seed)
        self.hours_step = hours_step
        self.now = start or datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
        self.model_sigma = model_sigma
        self.market_sigma = market_sigma
        self.markets: list[SimMarket] = []
        self._build(n_markets)
        self._refresh()  # set initial dynamic values

    def _build(self, n: int) -> None:
        for i in range(n):
            city, lat, lon = self._city(i)
            true_p = self.rng.uniform(0.2, 0.8)
            # ~40% of markets carry a meaningful mispricing we can exploit.
            bias = self.rng.choice(
                [0.0, 0.0, 0.0,
                 self.rng.uniform(0.10, 0.22), -self.rng.uniform(0.10, 0.22)])
            hours = self.rng.uniform(6, 120)
            thr = self.rng.choice([85, 88, 90, 92, 95])
            m = SimMarket(
                market_id=f"mkt-{i:02d}",
                question=f"Will the high temperature in {city.title()} be "
                         f"{thr}°F or above on the settlement day?",
                city=city, latitude=lat, longitude=lon, threshold_f=float(thr),
                yes_token_id=f"yes-{i:02d}", no_token_id=f"no-{i:02d}",
                settle_dt=self.now + timedelta(hours=hours),
                true_p=true_p, market_bias=bias,
                depth_shares=self.rng.choice([25, 30, 40, 60]),
            )
            self.markets.append(m)

    def _city(self, i: int):
        return _CITIES[i % len(_CITIES)]

    def _clip(self, x: float) -> float:
        return min(0.97, max(0.03, x))

    def _refresh(self) -> None:
        for m in self.markets:
            if m.resolved:
                continue
            m.model_prob = self._clip(m.true_p + self.rng.gauss(0, self.model_sigma))
            m.market_yes_fair = self._clip(
                m.true_p + m.market_bias + self.rng.gauss(0, self.market_sigma))

    def step(self) -> list[SimMarket]:
        """Advance one tick: time moves, model + market refresh, due markets settle."""
        self.now += timedelta(hours=self.hours_step)
        self._refresh()
        for m in self.markets:
            if not m.resolved and self.now >= m.settle_dt:
                m.event_outcome = self.rng.random() < m.true_p
                m.resolved = True
        return self.markets

    def active(self) -> list[SimMarket]:
        return [m for m in self.markets if not m.resolved]
