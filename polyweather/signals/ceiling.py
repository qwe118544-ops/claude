"""Thermodynamic ceiling: the forecaster's mixed-layer method.

If the boundary layer mixes up to pressure level L (height z_L, temp T_L),
the surface can at most reach T_L + Γd·(z_L − z_sfc) with Γd = 9.8 °C/km
(dry adiabat). We compute the bound from both 925 hPa (shallow mixing —
binding early in the day) and 850 hPa (deep mixing — the generous hard cap),
using the median across models of the latest fetched upper-air data.

Output: {'cap_925': float|None, 'cap_850': float|None} — the engine applies
cap_925 as a soft cap before local noon and cap_850 (+margin) all day.
"""
from __future__ import annotations

import datetime as dt
import statistics

from ..db import ModelForecast, get_session

GAMMA_D = 9.8  # °C per km


def evaluate(cfg, city_key: str, climate_date_str: str,
             now: dt.datetime | None = None) -> dict:
    city = cfg.cities[city_key]
    caps_925, caps_850 = [], []
    with get_session() as s:
        # newest fetch per model
        rows = (
            s.query(ModelForecast)
            .filter(ModelForecast.city == city_key,
                    ModelForecast.climate_date == climate_date_str)
            .order_by(ModelForecast.ts_fetch.desc())
            .limit(len(city.models) * 3)
            .all()
        )
    seen: set[str] = set()
    for r in rows:
        if r.model in seen or not r.upper:
            continue
        seen.add(r.model)
        t = r.hourly.get("time") or []
        # afternoon values (12-16h local) of the climate day
        idx = [i for i, ts in enumerate(t)
               if ts.startswith(climate_date_str) and 12 <= int(ts[11:13]) <= 16]
        for pl, out in (("925", caps_925), ("850", caps_850)):
            tv = r.upper.get(f"temperature_{pl}hPa")
            zv = r.upper.get(f"geopotential_height_{pl}hPa")
            if not tv or not zv:
                continue
            vals = []
            for i in idx:
                if i < len(tv) and tv[i] is not None and i < len(zv) and zv[i] is not None:
                    dz_km = (zv[i] - city.elevation_m) / 1000.0
                    vals.append(tv[i] + GAMMA_D * dz_km)
            if vals:
                out.append(statistics.median(vals))
    return {
        "cap_925": round(statistics.median(caps_925), 2) if caps_925 else None,
        "cap_850": round(statistics.median(caps_850), 2) if caps_850 else None,
        "n_models": len(seen),
    }
