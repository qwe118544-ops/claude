"""Live ensemble forecast -> P(daily max >= threshold).

Rung 1 of the modelling ladder: pull every ensemble member's hourly
temperature for the settlement day, take each member's daily max, and count
the fraction of members on the correct side of the threshold. With ~30-50
members this is a genuine probability, not a point guess.

Two refinements kept deliberately small (the rest of the ladder lives in the
README): Laplace smoothing so a unanimous ensemble doesn't claim 0/1, and an
optional additive bias correction for the settlement station.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np


def probability_from_members(
    member_daily_max: np.ndarray,
    threshold: float,
    direction: str = ">=",
    bias_correction: float = 0.0,
    smoothing: float = 1.0,
) -> float:
    """Fraction of members satisfying the threshold, Laplace-smoothed.

    member_daily_max : array of each member's forecast daily max (already the
                       per-member max over the settlement day's hours).
    bias_correction  : added to every member (station vs model offset, in °F).
    smoothing        : Laplace pseudo-count; 1.0 => never exactly 0 or 1.
    """
    x = np.asarray(member_daily_max, dtype=float) + bias_correction
    n = len(x)
    if n == 0:
        raise ValueError("no ensemble members")
    if direction == ">=":
        hits = float(np.sum(x >= threshold))
    elif direction == "<=":
        hits = float(np.sum(x <= threshold))
    else:
        raise ValueError(f"bad direction: {direction}")
    return (hits + smoothing) / (n + 2 * smoothing)


@dataclass
class LiveEnsembleForecast:
    """Fetch ensemble member daily-maxes from Open-Meteo for one location/day."""

    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

    latitude: float
    longitude: float
    timezone_name: str = "auto"
    models: str = "gfs_seamless"  # add e.g. "icon_seamless,ecmwf_ifs025" to blend
    timeout: int = 60
    session: object = None

    def fetch_member_daily_max(self, target: date) -> np.ndarray:
        """Return an array of each member's max temperature_2m on `target`."""
        import requests  # lazy import; offline use never touches the network

        sess = self.session or requests
        iso = target.isoformat()
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "hourly": "temperature_2m",
            "models": self.models,
            "timezone": self.timezone_name,
            "start_date": iso,
            "end_date": iso,
        }
        resp = sess.get(self.ENSEMBLE_URL, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Open-Meteo ensemble request failed: HTTP {resp.status_code} "
                f"- {resp.text[:200]}"
            )
        hourly = resp.json()["hourly"]
        # Member columns look like temperature_2m_member01, _member02, ...
        # Some models also expose a control run as plain temperature_2m.
        member_keys = [k for k in hourly if k.startswith("temperature_2m")]
        if not member_keys:
            raise RuntimeError("no temperature_2m member columns returned")
        maxes = []
        for k in member_keys:
            col = np.asarray([v for v in hourly[k] if v is not None], dtype=float)
            if col.size:
                maxes.append(col.max())
        return np.asarray(maxes, dtype=float)

    def probability(
        self,
        target: date,
        threshold: float,
        direction: str = ">=",
        bias_correction: float = 0.0,
    ) -> float:
        members = self.fetch_member_daily_max(target)
        return probability_from_members(
            members, threshold, direction=direction, bias_correction=bias_correction
        )
