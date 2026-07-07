"""k-NN analog model: 'in this situation, how much higher than the current
running max did the day eventually get?'

Uses the per-city daily trajectory matrix built by `polyweather backfill`
(data/climatology/{city}_days.parquet): one row per historical day with the
temperature at each half-hour local slot, the day's final max, and day-of-year.

Features for neighbour distance:
  - day-of-year (circular, hard window ±45 d)
  - temp now (at the current half-hour slot)
  - running max so far
  - 3 h warming slope
Output: empirical samples of (final_max − run_max_now) for the k neighbours.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import DATA_DIR


class AnalogModel:
    def __init__(self, city_key: str):
        self.city_key = city_key
        self._df: pd.DataFrame | None = None
        self._loaded = False

    def _load(self) -> pd.DataFrame | None:
        if not self._loaded:
            path = DATA_DIR / "climatology" / f"{self.city_key}_days.parquet"
            self._df = pd.read_parquet(path) if path.exists() else None
            self._loaded = True
        return self._df

    @property
    def available(self) -> bool:
        return self._load() is not None

    def residual_samples(self, doy: int, slot: int, temp_now: float,
                         run_max: float, slope_3h: float, k: int = 60) -> np.ndarray:
        """slot = local half-hour index (0..47). Returns array of
        final_max - run_max_at_slot for the k nearest historical days."""
        df = self._load()
        if df is None or slot < 6:
            return np.array([])
        tcol, rcol = f"t{slot}", f"rmax{slot}"
        scol = f"t{max(slot - 6, 0)}"
        need = {tcol, rcol, scol, "final_max", "doy"}
        if not need.issubset(df.columns):
            return np.array([])
        d = df.dropna(subset=[tcol, rcol, scol, "final_max"])
        if d.empty:
            return np.array([])
        # circular day-of-year window ±45
        delta = (d["doy"] - doy).abs()
        delta = np.minimum(delta, 365 - delta)
        d = d[delta <= 45]
        if len(d) < 10:
            return np.array([])
        f_temp = (d[tcol] - temp_now) / 2.0
        f_rmax = (d[rcol] - run_max) / 2.0
        f_slope = ((d[tcol] - d[scol]) - slope_3h) / 1.5
        dist = np.sqrt(f_temp ** 2 + f_rmax ** 2 + f_slope ** 2)
        idx = dist.nsmallest(min(k, len(d))).index
        sel = d.loc[idx]
        return (sel["final_max"] - sel[rcol]).to_numpy(dtype=float)


_models: dict[str, AnalogModel] = {}


def get_analog(city_key: str) -> AnalogModel:
    if city_key not in _models:
        _models[city_key] = AnalogModel(city_key)
    return _models[city_key]
