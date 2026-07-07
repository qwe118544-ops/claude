"""Nightly settlement + scoring.

After a city's climate day closes, compute the settlement integer from stored
METAR observations (same basis as the Wunderground history table), then score
every PricingVersion of that day at hourly checkpoints: Brier score over
integer buckets and log-loss of the realized integer. This page is the
paper-trading verdict — it decides whether the edge is real.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from .clock import climate_day_bounds_utc
from .db import DailyOutcome, Observation, PricingVersion, get_session


def settle_city_day(cfg, city_key: str, climate_date_str: str) -> dict | None:
    city = cfg.cities[city_key]
    start, end = climate_day_bounds_utc(city, climate_date_str)
    start_n, end_n = start.replace(tzinfo=None), end.replace(tzinfo=None)
    with get_session() as s:
        obs = (
            s.query(Observation)
            .filter(Observation.city == city_key,
                    Observation.station == city.icao,
                    Observation.ts_obs >= start_n, Observation.ts_obs < end_n,
                    Observation.temp.isnot(None))
            .order_by(Observation.ts_obs)
            .all()
        )
        if not obs:
            return None
        settle = max(int(round(o.temp)) for o in obs)
        peak_obs = max(obs, key=lambda o: o.temp)
        tz = ZoneInfo(city.tz)
        peak_local = peak_obs.ts_obs.replace(tzinfo=dt.timezone.utc).astimezone(tz)

        versions = (
            s.query(PricingVersion)
            .filter(PricingVersion.city == city_key,
                    PricingVersion.climate_date == climate_date_str)
            .order_by(PricingVersion.ts)
            .all()
        )
        scores: dict[str, dict] = {}
        for hour in range(0, 24):
            cutoff = start_n + dt.timedelta(hours=hour)
            cand = [v for v in versions if v.ts <= cutoff]
            if not cand:
                continue
            v = cand[-1]
            pmf = {int(k): float(p) for k, p in v.int_pmf.items()}
            brier = sum((p - (1.0 if j == settle else 0.0)) ** 2 for j, p in pmf.items())
            p_true = max(pmf.get(settle, 0.0), 1e-4)
            scores[str(hour)] = {
                "brier": round(brier, 4),
                "logloss": round(-math.log(p_true), 4),
                "p_settle": round(pmf.get(settle, 0.0), 4),
                "version_id": v.id,
            }

        existing = (
            s.query(DailyOutcome)
            .filter_by(city=city_key, climate_date=climate_date_str)
            .first()
        )
        if existing:
            existing.settle_int = settle
            existing.peak_time_local = peak_local.strftime("%H:%M")
            existing.scores = scores
        else:
            s.add(DailyOutcome(
                city=city_key, climate_date=climate_date_str,
                settle_int=settle,
                peak_time_local=peak_local.strftime("%H:%M"),
                scores=scores,
            ))
        s.commit()
    return {"city": city_key, "date": climate_date_str, "settle": settle,
            "peak_local": peak_local.strftime("%H:%M"),
            "n_versions_scored": len(scores)}


def settle_yesterday_all(cfg) -> list[dict]:
    out = []
    for key, city in cfg.cities.items():
        tz = ZoneInfo(city.tz)
        yday = (dt.datetime.now(tz) - dt.timedelta(days=1)).date().isoformat()
        r = settle_city_day(cfg, key, yday)
        if r:
            out.append(r)
    return out
