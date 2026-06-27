"""Order, fill and position value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OrderType(Enum):
    FOK = "FOK"   # fill-or-kill: all of it within the limit, or nothing
    FAK = "FAK"   # fill-and-kill (IOC): take what's there now, cancel the rest


class Side(Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL = "SELL"     # exit an existing share position into the bid side


@dataclass
class Order:
    market_id: str
    token_id: str
    side: Side
    order_type: OrderType
    # Entry orders are sized in USDC budget; exits are sized in shares.
    budget_usdc: Optional[float] = None
    shares: Optional[float] = None
    limit_price: Optional[float] = None   # worst acceptable per-share price
    signal_time: float = 0.0              # simulated clock (seconds)
    ref_price: Optional[float] = None     # best price at signal time (slippage baseline)


@dataclass
class Fill:
    order: Order
    arrival_time: float
    latency_ms: float
    filled_shares: float
    filled_notional: float        # USDC actually spent (buy) or received (sell)
    avg_price: float              # VWAP of the fill
    fee: float
    requested_notional: float
    unfilled_notional: float
    slippage_per_share: float     # signed: positive = worse than ref price
    status: str                   # FILLED | PARTIAL | CANCELLED

    @property
    def unfilled_rate(self) -> float:
        if self.requested_notional <= 0:
            return 0.0
        return max(0.0, self.unfilled_notional) / self.requested_notional


@dataclass
class Position:
    market_id: str
    token_id: str
    side: Side                    # BUY_YES or BUY_NO (the outcome we are long)
    shares: float = 0.0
    cost_basis: float = 0.0       # USDC paid for shares (excl. fees)
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    settled: bool = False
    settle_value: Optional[float] = None
    question: str = ""
    fills: list = field(default_factory=list)

    def avg_price(self) -> float:
        return self.cost_basis / self.shares if self.shares > 0 else 0.0
