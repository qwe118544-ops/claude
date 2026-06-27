"""Continuous trading backend.

All discovery and betting logic lives here (server-side): refresh the model,
scan the whole market universe, size bets, and route them to the paper broker.
The dashboard is a pure view onto the state this engine publishes.
"""

from .sizing import size_bet, SizingParams
from .simfeed import SimMarket, SimUniverse
from .engine import TradingEngine, EngineConfig

__all__ = [
    "size_bet", "SizingParams",
    "SimMarket", "SimUniverse",
    "TradingEngine", "EngineConfig",
]
