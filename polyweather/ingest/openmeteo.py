"""Open-Meteo multi-model forecast ingestor (free, no key).

One request per city fetches every configured model's hourly curve for the
resolution-station coordinates, plus 925/850 hPa temperature and geopotential
height (for the thermodynamic-ceiling signal). Each fetch is stored; the
momentum signal is computed from OUR OWN stored history of fetches, so no
'previous runs' API is needed.
"""
from __future__ import annotations

import datetime as dt

from ..clock import climate_date
from ..db import ModelForecast, get_session
from .base import BaseIngestor, utcnow

API = "https://api.open-meteo.com/v1/forecast"

HOURLY = ",".join([
    "temperature_2m", "dew_point_2m", "wind_speed_10m", "wind_direction_10m",
    "cloud_cover",
])
UPPER = ",".join([
    "temperature_925hPa", "temperature_850hPa",
    "geopotential_height_925hPa", "geopotential_height_850hPa",
])


class OpenMeteoIngestor(BaseIngestor):
    name = "openmeteo"

    async def poll_city(self, city_key: str) -> bool:
        city = self.cfg.cities[city_key]
        params = {
            "latitude": city.lat, "longitude": city.lon,
            "hourly": f"{HOURLY},{UPPER}",
            "models": ",".join(city.models),
            "timezone": city.tz,
            "forecast_days": 2,
            "wind_speed_unit": "ms",
        }
        try:
            r = await self.get(API, params=params)
            data = r.json()
        except Exception as e:
            self.log_latency(None, ok=False, note=f"{city_key}: {type(e).__name__}: {e}")
            return False

        cdate = climate_date(city)
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        stored = 0
        with get_session() as s:
            for model in city.models:
                curve = self._model_series(hourly, "temperature_2m", model, city.models)
                if curve is None:
                    continue
                today = [
                    (t, v) for t, v in zip(times, curve)
                    if t.startswith(cdate) and v is not None
                ]
                today_max = max((v for _, v in today), default=None)
                upper = {
                    k: self._model_series(hourly, k, model, city.models)
                    for k in UPPER.split(",")
                }
                payload_hourly = {
                    "time": times,
                    "temperature_2m": curve,
                    "cloud_cover": self._model_series(hourly, "cloud_cover", model, city.models),
                    "wind_direction_10m": self._model_series(hourly, "wind_direction_10m", model, city.models),
                }
                s.add(ModelForecast(
                    city=city_key, model=model, ts_fetch=utcnow(),
                    climate_date=cdate, today_max=today_max,
                    hourly=payload_hourly,
                    upper={k: v for k, v in upper.items() if v is not None},
                ))
                stored += 1
            s.commit()
        self.log_latency(utcnow(), ok=True, note=f"{city_key}: {stored} models")
        return stored > 0

    @staticmethod
    def _model_series(hourly: dict, var: str, model: str, models: list[str]):
        """Open-Meteo suffixes variables with the model name when several
        models are requested ('temperature_2m_ecmwf_ifs025'); with a single
        model the plain name is used."""
        if len(models) == 1:
            return hourly.get(var)
        return hourly.get(f"{var}_{model}")

    async def poll(self) -> list[str]:
        changed = []
        for key in self.cfg.cities:
            if await self.poll_city(key):
                changed.append(key)
        return changed
