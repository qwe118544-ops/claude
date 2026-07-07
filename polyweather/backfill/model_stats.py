"""Per-city, per-model bias/σ from Open-Meteo's historical *forecast* archive
(what the model actually predicted at the time, not reanalysis) vs the IEM
settlement value. Writes data/climatology/model_stats.json used by the engine.

The archive holds day-0 forecasts, so this calibrates the 'morning state':
sigma_by_hours_left is scaled from the day-0 error using the default decay
shape. Good enough until weeks of own-fetch history accumulate.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics

import httpx
import pandas as pd

from ..config import DATA_DIR, USER_AGENT

HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
DECAY = {12: 1.0, 8: 0.86, 6: 0.71, 4: 0.57, 2: 0.39, 1: 0.29, 0: 0.21}


def run(cfg, days: int = 365) -> None:
    end = dt.date.today() - dt.timedelta(days=2)
    start = end - dt.timedelta(days=days)
    out: dict = {}
    with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as client:
        for key, city in cfg.cities.items():
            actual = _actuals(key)
            if actual is None:
                print(f"{key}: run `polyweather backfill iem` first — skipping")
                continue
            out[key] = {}
            for model in city.models:
                r = client.get(HIST, params={
                    "latitude": city.lat, "longitude": city.lon,
                    "daily": "temperature_2m_max", "models": model,
                    "timezone": city.tz,
                    "start_date": start.isoformat(), "end_date": end.isoformat(),
                })
                if r.status_code != 200:
                    print(f"  {key}/{model}: HTTP {r.status_code} — skipped")
                    continue
                d = r.json().get("daily") or {}
                errs = []
                for date, fmax in zip(d.get("time") or [], d.get("temperature_2m_max") or []):
                    if fmax is None:
                        continue
                    a = actual.get(date)
                    if a is not None:
                        errs.append(a - fmax)
                if len(errs) < 60:
                    print(f"  {key}/{model}: only {len(errs)} pairs — skipped")
                    continue
                bias = statistics.mean(errs)
                sigma0 = statistics.pstdev(errs)
                out[key][model] = {
                    "bias": round(bias, 2),
                    "sigma_day0": round(sigma0, 2),
                    "n": len(errs),
                    "sigma_by_hours_left": {
                        str(h): round(max(sigma0 * f, 0.25), 2) for h, f in DECAY.items()
                    },
                }
                print(f"  {key}/{model}: bias {bias:+.2f} sigma {sigma0:.2f} (n={len(errs)})")
    path = DATA_DIR / "climatology" / "model_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {path}")


def _actuals(city_key: str) -> dict[str, float] | None:
    path = DATA_DIR / "climatology" / f"{city_key}_days.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["date", "final_max"])
    return {row.date: round(float(row.final_max)) for row in df.itertuples()}
