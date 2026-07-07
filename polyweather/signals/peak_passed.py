"""P(the day's max has already occurred).

Primary source: the station's own climatology of peak-time by month, built by
`polyweather backfill` from 10+ years of IEM METAR history (fraction of days
whose peak occurred at or before the current local time). Until backfill has
run, a logistic fallback centred on the configured peak window is used.

Adjustments on top of the base rate:
  + breeze front arrived            -> floor at 0.85
  + temp falling off the running max (>=0.7 °C for >=2 readings) -> floor 0.8
  - temp within 0.3 °C of running max and still rising -> cap at 0.35
"""
from __future__ import annotations

import datetime as dt
import json
import math

from ..clock import local_now, peak_center_minutes
from ..config import DATA_DIR
from ..db import Observation, get_session


def _climatology(city_key: str) -> dict | None:
    path = DATA_DIR / "climatology" / f"{city_key}_peaktime.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def evaluate(cfg, city_key: str, now: dt.datetime, breeze: dict) -> dict:
    city = cfg.cities[city_key]
    loc = local_now(city, now.replace(tzinfo=dt.timezone.utc))
    minutes = loc.hour * 60 + loc.minute
    month = str(loc.month)

    clim = _climatology(city_key)
    if clim and month in clim:
        # cumulative fraction of historical days with peak <= now, per month,
        # sampled every 30 min: clim[month] = [f_0000, f_0030, ...]
        arr = clim[month]
        idx = min(minutes // 30, len(arr) - 1)
        base = float(arr[idx])
        source = "climatology"
    else:
        center = peak_center_minutes(city, cfg.summer_months, loc.month)
        base = 1.0 / (1.0 + math.exp(-(minutes - center) / 60.0))
        source = "fallback_logistic"

    p = base
    with get_session() as s:
        rows = (
            s.query(Observation)
            .filter(Observation.city == city_key,
                    Observation.station == city.icao,
                    Observation.ts_obs >= now - dt.timedelta(minutes=120),
                    Observation.temp.isnot(None))
            .order_by(Observation.ts_obs)
            .all()
        )
    if rows:
        temps = [r.temp for r in rows]
        run_max = max(temps)
        last = temps[-1]
        falling = len(temps) >= 3 and temps[-1] <= temps[-2] <= temps[-3] \
            and (run_max - last) >= 0.7
        rising_near_max = (run_max - last) <= 0.3 and len(temps) >= 2 \
            and temps[-1] > temps[-2]
        if falling:
            p = max(p, 0.8)
        if rising_near_max:
            p = min(p, 0.35)
    if breeze.get("arrived"):
        p = max(p, 0.85)
    return {"p": round(min(max(p, 0.0), 0.99), 3), "base": round(base, 3),
            "source": source}
