"""Per-market fee model.

Fees are read dynamically per market rather than hard-coded: Polymarket has
historically run 0% trading fees on many markets but charges on others and the
schedule can change, so the engine looks up the rate per token and the
simulator applies whatever it is told. Supply a resolver that returns the
proportional fee (e.g. 0.0 for none, 0.02 for 2%) for a given market/token.
"""

from __future__ import annotations

from typing import Callable, Optional


class FeeModel:
    def __init__(self, resolver: Optional[Callable[[str, str], float]] = None,
                 default_rate: float = 0.0) -> None:
        # resolver(market_id, token_id) -> proportional fee on notional
        self._resolver = resolver
        self.default_rate = default_rate

    def rate(self, market_id: str, token_id: str) -> float:
        if self._resolver is not None:
            try:
                r = self._resolver(market_id, token_id)
                if r is not None:
                    return float(r)
            except Exception:
                pass
        return self.default_rate

    def fee(self, market_id: str, token_id: str, notional: float) -> float:
        return self.rate(market_id, token_id) * abs(notional)
