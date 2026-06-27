"""Forecast -> calibrated threshold probability.

The headline model is EMOS-lite (a minimal Ensemble Model Output Statistics
post-processor). Given a point forecast of the daily maximum, it:

    1. Bias-corrects with a linear fit:  mu = a + b * forecast
       (fit on the TRAIN split only -> no lookahead).
    2. Treats the residual as Gaussian with a learned spread sigma.
    3. Returns  P(max >= threshold) = 1 - Phi((threshold - mu) / sigma).

This is deliberately the *first rung* of the real ladder (the README documents
the rest: multi-model blending, station downscaling, intraday conditioning,
proper ensemble post-processing). It is enough to answer the only question
that matters first: is there calibrated skill beyond climatology?

`ClimatologyBaseline` is the honest yardstick — if EMOS can't beat "the event
happens at its historical base rate", there is no edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

_SQRT2 = math.sqrt(2.0)
# Vectorised standard-normal CDF using stdlib erf (no scipy dependency).
_erf_vec = np.vectorize(math.erf, otypes=[float])


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf_vec(np.asarray(x, dtype=float) / _SQRT2))


@dataclass
class EmosThresholdModel:
    """Linear bias correction + homoscedastic Gaussian spread."""

    a: float = 0.0
    b: float = 1.0
    sigma: float = 1.0
    fitted: bool = False

    def fit(self, forecast: np.ndarray, truth: np.ndarray) -> "EmosThresholdModel":
        forecast = np.asarray(forecast, dtype=float)
        truth = np.asarray(truth, dtype=float)
        if len(forecast) < 10:
            raise ValueError("need >= 10 training points to fit EMOS")

        # Least-squares: truth ~ a + b * forecast.
        design = np.column_stack([np.ones_like(forecast), forecast])
        coef, *_ = np.linalg.lstsq(design, truth, rcond=None)
        self.a, self.b = float(coef[0]), float(coef[1])

        residual = truth - (self.a + self.b * forecast)
        # ddof=2 for the two estimated regression parameters.
        self.sigma = float(np.sqrt(np.sum(residual**2) / max(len(residual) - 2, 1)))
        self.sigma = max(self.sigma, 1e-6)
        self.fitted = True
        return self

    def corrected_forecast(self, forecast: np.ndarray) -> np.ndarray:
        return self.a + self.b * np.asarray(forecast, dtype=float)

    def predict_proba_ge(self, forecast: np.ndarray, threshold: float) -> np.ndarray:
        """P(daily max >= threshold) for each forecast."""
        if not self.fitted:
            raise RuntimeError("model not fitted")
        mu = self.corrected_forecast(forecast)
        # P(X >= t) for X ~ N(mu, sigma) = 1 - Phi((t - mu)/sigma).
        z = (threshold - mu) / self.sigma
        p = 1.0 - _norm_cdf(z)
        return np.clip(p, 1e-6, 1 - 1e-6)


@dataclass
class ClimatologyBaseline:
    """Constant probability = event base rate in the training period."""

    rate: float = 0.5
    fitted: bool = False

    def fit(self, truth: np.ndarray, threshold: float) -> "ClimatologyBaseline":
        truth = np.asarray(truth, dtype=float)
        self.rate = float(np.clip((truth >= threshold).mean(), 1e-6, 1 - 1e-6))
        self.fitted = True
        return self

    def predict_proba_ge(self, n: int) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError("baseline not fitted")
        return np.full(n, self.rate, dtype=float)
