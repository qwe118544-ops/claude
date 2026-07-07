"""Trajectory residual: is today following the script?

Compares the resolution station's observed temps over the last 2 h with the
model-blend expectation at those times. Output feeds the engine as a mean
shift (models running warm/cold today) and a widen factor (script broken).
"""
from __future__ import annotations

import datetime as dt

from ..db import ModelForecast, Observation, get_session

WINDOW_MIN = 120


def evaluate(cfg, city_key: str, climate_date_str: str,
             now: dt.datetime) -> dict:
    city = cfg.cities[city_key]
    with get_session() as s:
        since = now - dt.timedelta(minutes=WINDOW_MIN)
        obs = (
            s.query(Observation)
            .filter(Observation.city == city_key,
                    Observation.station == city.icao,
                    Observation.ts_obs >= since,
                    Observation.temp.isnot(None))
            .order_by(Observation.ts_obs)
            .all()
        )
        rows = (
            s.query(ModelForecast)
            .filter(ModelForecast.city == city_key,
                    ModelForecast.climate_date == climate_date_str)
            .order_by(ModelForecast.ts_fetch.desc())
            .limit(len(city.models) * 2)
            .all()
        )
    if len(obs) < 2 or not rows:
        return {"mean_residual": 0.0, "trend": 0.0, "n_obs": len(obs)}

    # blend expectation: mean over newest fetch of each model
    seen: set[str] = set()
    curves = []
    for r in rows:
        if r.model in seen:
            continue
        seen.add(r.model)
        t = r.hourly.get("time") or []
        v = r.hourly.get("temperature_2m") or []
        curves.append({ts: val for ts, val in zip(t, v) if val is not None})
    if not curves:
        return {"mean_residual": 0.0, "trend": 0.0, "n_obs": len(obs)}

    residuals = []
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(city.tz)
    for o in obs:
        loc = o.ts_obs.replace(tzinfo=dt.timezone.utc).astimezone(tz)
        key = loc.strftime("%Y-%m-%dT%H:00")
        frac = loc.minute / 60.0
        nxt = (loc + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        exps = []
        for c in curves:
            if key in c:
                a = c[key]
                b = c.get(nxt, a)
                exps.append(a + (b - a) * frac)
        if exps:
            residuals.append((o.ts_obs, o.temp - sum(exps) / len(exps)))

    if not residuals:
        return {"mean_residual": 0.0, "trend": 0.0, "n_obs": len(obs)}
    vals = [r for _, r in residuals]
    mean_r = sum(vals) / len(vals)
    half = len(vals) // 2 or 1
    trend = (sum(vals[half:]) / len(vals[half:])) - (sum(vals[:half]) / len(vals[:half])) \
        if len(vals) >= 2 else 0.0
    return {"mean_residual": round(mean_r, 2), "trend": round(trend, 2),
            "n_obs": len(obs)}
