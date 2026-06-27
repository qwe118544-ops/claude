"""Live edge-detection engine for Polymarket weather markets.

This is the operating core ("正题"): on each pass it pulls the current weather
markets and their prices, computes our own calibrated probability from the
latest ensemble forecast, and flags markets where our probability and the
market price diverge enough to be tradeable.

It does NOT place orders. Execution (sizing + wallet + signing) is a separate,
later phase that should only be built after live signal logging shows a real
edge — so this engine instead *logs every signal* to build that live track
record going forward (which sidesteps the scarcity of historical weather
markets that makes a backtest weak).

Network note: the live HTTP clients talk to Open-Meteo and Polymarket. In
egress-restricted environments those hosts may be blocked; the *decision logic*
(forecast->probability, edge, filters, sizing of conviction) is fully unit
tested offline via injected fakes, so the brain is verifiable without network.
"""

from .markets import WeatherMarket, parse_weather_question
from .forecast import LiveEnsembleForecast, probability_from_members
from .signal import Signal, SignalParams, evaluate_signal
from .scanner import Scanner, MarketClient

__all__ = [
    "WeatherMarket",
    "parse_weather_question",
    "LiveEnsembleForecast",
    "probability_from_members",
    "Signal",
    "SignalParams",
    "evaluate_signal",
    "Scanner",
    "MarketClient",
]
