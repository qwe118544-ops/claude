"""Signal -> order-arrival latency model (default 300-1000 ms)."""

from __future__ import annotations

import random
from typing import Optional


class LatencyModel:
    def __init__(self, min_ms: float = 300.0, max_ms: float = 1000.0,
                 rng: Optional[random.Random] = None) -> None:
        if min_ms < 0 or max_ms < min_ms:
            raise ValueError("require 0 <= min_ms <= max_ms")
        self.min_ms = min_ms
        self.max_ms = max_ms
        self._rng = rng or random.Random()

    def sample_ms(self) -> float:
        return self._rng.uniform(self.min_ms, self.max_ms)
