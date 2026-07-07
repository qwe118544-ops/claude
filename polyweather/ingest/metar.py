"""METAR ingestor via NOAA aviationweather.gov data API (free, no key).

One request covers all configured stations (resolution + upstream METAR
sentinels for every city). JSON schema: list of decoded reports with
icaoId, reportTime/obsTime, temp, dewp, wdir, wspd (knots), rawOb.
"""
from __future__ import annotations

import datetime as dt

from ..db import get_session, upsert_observation
from .base import BaseIngestor, utcnow

API = "https://aviationweather.gov/api/data/metar"
KT_TO_MS = 0.514444


class MetarIngestor(BaseIngestor):
    name = "metar"

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        # station -> [(city, role)]
        self.stations: dict[str, list[tuple[str, str]]] = {}
        for key, city in cfg.cities.items():
            self.stations.setdefault(city.icao, []).append((key, "resolution"))
            for up in city.upstream:
                if up["kind"] == "metar":
                    self.stations.setdefault(up["id"], []).append((key, "upstream"))

    async def poll(self) -> list[str]:
        ids = ",".join(sorted(self.stations))
        try:
            r = await self.get(API, params={"ids": ids, "format": "json", "hours": 3})
            reports = r.json()
        except Exception as e:
            self.log_latency(None, ok=False, note=f"{type(e).__name__}: {e}")
            return []

        changed: set[str] = set()
        newest: dt.datetime | None = None
        with get_session() as s:
            for rep in reports or []:
                icao = rep.get("icaoId")
                if icao not in self.stations:
                    continue
                ts = self._obs_time(rep)
                if ts is None:
                    continue
                temp = _num(rep.get("temp"))
                row = dict(
                    source="metar", station=icao, ts_obs=ts, ts_fetch=utcnow(),
                    temp=temp, dewp=_num(rep.get("dewp")),
                    wdir=_num(rep.get("wdir")),
                    wspd=(_num(rep.get("wspd")) or 0) * KT_TO_MS if rep.get("wspd") is not None else None,
                    rh=None, raw=(rep.get("rawOb") or "")[:500],
                )
                for city_key, _role in self.stations[icao]:
                    if upsert_observation(s, city=city_key, **row):
                        changed.add(city_key)
                if newest is None or ts > newest:
                    newest = ts
            s.commit()
        self.log_latency(newest, ok=True, note=f"{len(reports or [])} reports")
        return sorted(changed)

    @staticmethod
    def _obs_time(rep: dict) -> dt.datetime | None:
        # aviationweather returns epoch seconds in obsTime and/or ISO in reportTime
        v = rep.get("obsTime")
        if isinstance(v, (int, float)):
            return dt.datetime.utcfromtimestamp(v)
        for k in ("reportTime", "receiptTime"):
            v = rep.get(k)
            if isinstance(v, str):
                try:
                    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")) \
                        .astimezone(dt.timezone.utc).replace(tzinfo=None)
                except ValueError:
                    continue
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
