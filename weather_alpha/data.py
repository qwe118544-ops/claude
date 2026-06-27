"""Data sources that yield aligned (forecast, truth) pairs at a fixed lead time.

Every source returns the same tidy schema so the rest of the pipeline is
source-agnostic:

    target_date   datetime64   the day being forecast / settled
    lead_days     int          forecast was issued this many days before target_date
    forecast_max  float        forecast of daily max temp, issued at target_date - lead_days
    truth_max     float        observed daily max temp for target_date

NO-LOOKAHEAD CONTRACT: a row's `forecast_max` must only use information
available at (target_date - lead_days). Each source is responsible for
honouring this; `SyntheticSource` does it by construction, `OpenMeteoSource`
does it by reading archived previous model runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Columns every source must produce.
SCHEMA = ["target_date", "lead_days", "forecast_max", "truth_max"]


@dataclass
class ForecastTruthFrame:
    """Thin wrapper around the tidy DataFrame, with validation."""

    df: pd.DataFrame

    def __post_init__(self) -> None:
        missing = [c for c in SCHEMA if c not in self.df.columns]
        if missing:
            raise ValueError(f"frame missing required columns: {missing}")
        self.df = self.df[SCHEMA].copy()
        self.df["target_date"] = pd.to_datetime(self.df["target_date"])
        self.df = self.df.dropna(subset=["forecast_max", "truth_max"])
        self.df = self.df.sort_values("target_date").reset_index(drop=True)

    def __len__(self) -> int:
        return len(self.df)


class DataSource(ABC):
    """Yields a ForecastTruthFrame for a single (location, lead_days)."""

    @abstractmethod
    def fetch(self) -> ForecastTruthFrame:  # pragma: no cover - interface
        ...


# --------------------------------------------------------------------------- #
# Synthetic source: a known generative error model, for offline validation.
# --------------------------------------------------------------------------- #
class SyntheticSource(DataSource):
    """Generate (forecast, truth) pairs from a *known* error model.

    Truth follows a seasonal climatology plus an AR(1) weather anomaly. The
    forecast equals truth plus a lead-dependent bias and Gaussian spread:

        forecast = truth + bias(lead) + N(0, spread(lead))

    Because the error model is known, a correctly implemented EMOS calibrator
    should recover near-perfect reliability and beat climatology. That is how
    we prove the *framework* is correct without any network access.
    """

    def __init__(
        self,
        n_days: int = 400,
        lead_days: int = 1,
        start_date: str = "2023-04-01",
        annual_mean_f: float = 62.0,
        seasonal_amplitude_f: float = 22.0,
        peak_doy: int = 196,          # ~mid-July, hottest day of year
        anomaly_phi: float = 0.7,     # AR(1) persistence of weather anomaly
        anomaly_sigma_f: float = 4.5,
        obs_noise_f: float = 1.0,
        bias_per_lead_f: float = 0.5,    # forecast runs warm, growing with lead
        spread_base_f: float = 1.5,
        spread_per_lead_f: float = 1.2,  # forecast spread grows with lead
        seed: int = 7,
    ) -> None:
        self.n_days = n_days
        self.lead_days = lead_days
        self.start_date = start_date
        self.annual_mean_f = annual_mean_f
        self.seasonal_amplitude_f = seasonal_amplitude_f
        self.peak_doy = peak_doy
        self.anomaly_phi = anomaly_phi
        self.anomaly_sigma_f = anomaly_sigma_f
        self.obs_noise_f = obs_noise_f
        self.bias_per_lead_f = bias_per_lead_f
        self.spread_base_f = spread_base_f
        self.spread_per_lead_f = spread_per_lead_f
        self.seed = seed

    def _seasonal_mean(self, doy: np.ndarray) -> np.ndarray:
        return self.annual_mean_f + self.seasonal_amplitude_f * np.cos(
            2 * np.pi * (doy - self.peak_doy) / 365.25
        )

    def fetch(self) -> ForecastTruthFrame:
        rng = np.random.default_rng(self.seed)
        dates = pd.date_range(self.start_date, periods=self.n_days, freq="D")
        doy = dates.dayofyear.to_numpy()

        # AR(1) weather anomaly.
        anomaly = np.zeros(self.n_days)
        innov_sigma = self.anomaly_sigma_f * np.sqrt(1 - self.anomaly_phi**2)
        for t in range(1, self.n_days):
            anomaly[t] = self.anomaly_phi * anomaly[t - 1] + rng.normal(0, innov_sigma)

        truth = self._seasonal_mean(doy) + anomaly + rng.normal(0, self.obs_noise_f, self.n_days)

        bias = self.bias_per_lead_f * self.lead_days
        spread = self.spread_base_f + self.spread_per_lead_f * self.lead_days
        forecast = truth + bias + rng.normal(0, spread, self.n_days)

        df = pd.DataFrame(
            {
                "target_date": dates,
                "lead_days": self.lead_days,
                "forecast_max": np.round(forecast, 1),
                "truth_max": np.round(truth, 1),
            }
        )
        return ForecastTruthFrame(df)

    def true_probability_ge(self, threshold: float) -> float:
        """The *true* model-implied event rate, useful for sanity checks."""
        # Marginal over the season is messy; this is only a rough reference.
        f = self.fetch().df
        return float((f["truth_max"] >= threshold).mean())


# --------------------------------------------------------------------------- #
# Open-Meteo source: live, archived previous model runs (no-lookahead).
# --------------------------------------------------------------------------- #
class OpenMeteoSource(DataSource):
    """Pull aligned forecast/truth from Open-Meteo's free archive APIs.

    Truth comes from the ERA5 reanalysis archive; the forecast at a given lead
    comes from the "previous model runs" feature, which exposes, for each date,
    the value that was forecast `lead_days` earlier — i.e. genuinely the data
    available at issue time.

    NOTE ON ACCESS: in some managed/egress-restricted environments the host
    `open-meteo.com` may be blocked by network policy; the request will then
    fail loudly. Nothing else in the framework depends on network access — use
    `SyntheticSource` to validate the pipeline offline.

    CAVEATS you must respect before trusting any result:
      * ERA5 reanalysis is NOT the ASOS/METAR station a contract settles on.
        For real trading, replace the truth source with the exact settlement
        station's official daily max (see README "Settlement source").
      * The standard forecast endpoint exposes ~92 past days and previous runs
        up to ~7 days. For multi-year backtests, point `base_url` at the
        Historical Forecast API and extend the previous-run handling.
    """

    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        latitude: float,
        longitude: float,
        start_date: str,
        end_date: str,
        lead_days: int = 1,
        timezone: str = "auto",
        timeout: int = 60,
        session=None,
    ) -> None:
        if not 1 <= lead_days <= 7:
            raise ValueError("previous-runs lead_days must be in 1..7 for the standard endpoint")
        self.latitude = latitude
        self.longitude = longitude
        self.start_date = start_date
        self.end_date = end_date
        self.lead_days = lead_days
        self.timezone = timezone
        self.timeout = timeout
        self._session = session

    def _get(self, url: str, params: dict) -> dict:
        import requests  # imported lazily so offline use needs no network stack

        sess = self._session or requests
        resp = sess.get(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Open-Meteo request to {url} failed: HTTP {resp.status_code} "
                f"— {resp.text[:200]}"
            )
        return resp.json()

    def _fetch_truth(self) -> pd.DataFrame:
        data = self._get(
            self.ARCHIVE_URL,
            {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "daily": "temperature_2m_max",
                "timezone": self.timezone,
            },
        )
        daily = data["daily"]
        return pd.DataFrame(
            {
                "target_date": pd.to_datetime(daily["time"]),
                "truth_max": daily["temperature_2m_max"],
            }
        )

    def _fetch_forecast(self) -> pd.DataFrame:
        # `temperature_2m_max_previous_dayN` gives, for each date, the forecast
        # issued N days earlier — exactly the no-lookahead value we want.
        var = f"temperature_2m_max_previous_day{self.lead_days}"
        data = self._get(
            self.FORECAST_URL,
            {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "daily": f"temperature_2m_max,{var}",
                "past_days": 92,
                "forecast_days": 1,
                "timezone": self.timezone,
            },
        )
        daily = data["daily"]
        return pd.DataFrame(
            {
                "target_date": pd.to_datetime(daily["time"]),
                "forecast_max": daily[var],
            }
        )

    def fetch(self) -> ForecastTruthFrame:
        truth = self._fetch_truth()
        forecast = self._fetch_forecast()
        merged = pd.merge(forecast, truth, on="target_date", how="inner")
        merged["lead_days"] = self.lead_days
        return ForecastTruthFrame(merged)
