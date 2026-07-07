"""JMA AMeDAS ingestor (free, no key, 10-minute cadence).

Endpoints (the ones the JMA website itself uses):
  latest_time.txt                      -> newest available timestamp
  const/amedastable.json               -> station table {id: {kjName, ...}}
  data/map/{yyyyMMddHHmmss}.json       -> all stations at that time

Station IDs are NEVER hard-coded: config lists station names in kanji
(e.g. 羽田, 江戸川臨海) and we resolve IDs from amedastable.json at runtime.
"""
from __future__ import annotations

import datetime as dt

from ..db import get_session, upsert_observation
from .base import BaseIngestor, dewpoint_from_rh, utcnow

BASE = "https://www.jma.go.jp/bosai/amedas"
# AMeDAS windDirection: 1..16 compass points (16 = N), 0 = calm
DIR_STEP = 22.5


class AmedasIngestor(BaseIngestor):
    name = "amedas"

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self._id_by_name: dict[str, str] = {}
        # name -> [(city, role)]
        self.wanted: dict[str, list[tuple[str, str]]] = {}
        for key, city in cfg.cities.items():
            for fs in city.fast_sources:
                if fs["kind"] == "amedas":
                    self.wanted.setdefault(fs["station_name"], []).append((key, "resolution_fast"))
            for up in city.upstream:
                if up["kind"] == "amedas":
                    self.wanted.setdefault(up["id"], []).append((key, "upstream"))

    async def resolve_ids(self) -> dict[str, str]:
        """kanji name -> numeric id, from the official station table."""
        if self._id_by_name:
            return self._id_by_name
        r = await self.get(f"{BASE}/const/amedastable.json")
        table = r.json()
        for sid, meta in table.items():
            name = meta.get("kjName") or meta.get("knName") or ""
            if name in self.wanted and name not in self._id_by_name:
                self._id_by_name[name] = sid
        missing = set(self.wanted) - set(self._id_by_name)
        if missing:
            raise RuntimeError(f"AMeDAS stations not found in amedastable.json: {missing}")
        return self._id_by_name

    async def poll(self) -> list[str]:
        if not self.wanted:
            return []
        try:
            ids = await self.resolve_ids()
            r = await self.get(f"{BASE}/data/latest_time.txt")
            latest = dt.datetime.fromisoformat(r.text.strip())
            stamp = latest.strftime("%Y%m%d%H%M%S")
            r = await self.get(f"{BASE}/data/map/{stamp}.json")
            data = r.json()
        except Exception as e:
            self.log_latency(None, ok=False, note=f"{type(e).__name__}: {e}")
            return []

        ts_utc = latest.astimezone(dt.timezone.utc).replace(tzinfo=None)
        changed: set[str] = set()
        with get_session() as s:
            for name, targets in self.wanted.items():
                sid = ids[name]
                rec = data.get(sid)
                if not rec:
                    continue
                temp = _qval(rec.get("temp"))
                rh = _qval(rec.get("humidity"))
                wdir16 = _qval(rec.get("windDirection"))
                wspd = _qval(rec.get("wind"))
                dewp = dewpoint_from_rh(temp, rh) if temp is not None and rh else None
                wdir = (wdir16 * DIR_STEP) % 360 if wdir16 else None
                row = dict(
                    source="amedas", station=sid, ts_obs=ts_utc, ts_fetch=utcnow(),
                    temp=temp, dewp=dewp, wdir=wdir, wspd=wspd, rh=rh,
                    raw=name,
                )
                for city_key, _role in targets:
                    if upsert_observation(s, city=city_key, **row):
                        changed.add(city_key)
            s.commit()
        self.log_latency(ts_utc, ok=True, note=f"stamp {stamp}")
        return sorted(changed)


def _qval(pair):
    """AMeDAS values come as [value, quality_flag]; flag 0 = OK."""
    if isinstance(pair, list) and len(pair) >= 2 and pair[1] == 0:
        return float(pair[0])
    return None
