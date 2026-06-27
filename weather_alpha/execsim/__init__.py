"""Realistic paper-trading execution simulator.

Models the things that actually determine whether a backtested edge survives
contact with a live order book:

  * a real-time order book you fill against level by level (VWAP), not at mid;
  * 300-1000 ms latency between signal and order arrival, during which the
    book moves (so latency causes genuine adverse slippage, not a cosmetic
    delay);
  * FOK (fill-or-kill) and FAK (fill-and-kill / IOC) order types, partial
    fills, and cancel-on-insufficient-liquidity;
  * per-market dynamic fee rates;
  * exits matched against the real bid side, level by level;
  * settlement to the binary outcome, with net P&L, slippage and unfilled-rate
    accounting.

All pure simulation — no network, fully unit tested.
"""

from .book import OrderBook, Level, fill_buy, fill_sell, FillWalk
from .orders import OrderType, Side, Order, Fill, Position
from .fees import FeeModel
from .latency import LatencyModel
from .feed import BookFeed, SimulatedBookFeed
from .broker import PaperBroker, ExecutionStats

__all__ = [
    "OrderBook", "Level", "fill_buy", "fill_sell", "FillWalk",
    "OrderType", "Side", "Order", "Fill", "Position",
    "FeeModel", "LatencyModel", "BookFeed", "SimulatedBookFeed",
    "PaperBroker", "ExecutionStats",
]
