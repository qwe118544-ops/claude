"""weather_alpha — a no-lookahead backtest + calibration framework for
threshold weather prediction markets (e.g. Polymarket "max temp >= X").

The single question this package answers, honestly:

    Under the contract's settlement definition, is my calibrated probability
    measurably better than the market price (and better than climatology)?

It does NOT place trades. It tells you whether an edge plausibly exists
*before* you risk money — which is the highest-leverage first step.

Design priorities (in order):
    1. No lookahead. The forecast used for target date D is the one that was
       actually available `lead_days` before D. Calibration parameters are fit
       on a strictly earlier train split than the test split they score.
    2. Evaluate calibration, not P&L. We score with proper scoring rules
       (Brier, log loss) and reliability diagrams — never noisy realized P&L.
    3. Pluggable data. The same pipeline runs on the live Open-Meteo source
       and on a synthetic source with a *known* error model, so the framework
       can be validated offline by checking it recovers correct calibration.
"""

from .data import DataSource, OpenMeteoSource, SyntheticSource, ForecastTruthFrame
from .model import EmosThresholdModel, ClimatologyBaseline
from .evaluation import (
    brier_score,
    log_loss,
    brier_skill_score,
    reliability_table,
    reliability_diagram_text,
)
from .backtest import run_backtest, BacktestResult

__all__ = [
    "DataSource",
    "OpenMeteoSource",
    "SyntheticSource",
    "ForecastTruthFrame",
    "EmosThresholdModel",
    "ClimatologyBaseline",
    "brier_score",
    "log_loss",
    "brier_skill_score",
    "reliability_table",
    "reliability_diagram_text",
    "run_backtest",
    "BacktestResult",
]

__version__ = "0.1.0"
