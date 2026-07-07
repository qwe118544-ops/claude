"""KNMI 10-minute observations ingestor (free, needs API key).

Get a free key (or use the public anonymous key) at:
https://developer.dataplatform.knmi.nl/open-data-api  -> env KNMI_API_KEY

Flow: list newest file of dataset Actuele10mindataKNMIstations v2 ->
temporary download URL -> netCDF -> extract configured stations.
Variables in the file: station, ta (air temp °C), td (dewpoint), rh,
dd (wind dir), ff (wind speed m/s); station codes like '06240' (Schiphol).
"""
from __future__ import annotations

import datetime as dt
import io
import tempfile

import numpy as np

from ..config import KNMI_API_KEY
from ..db import get_session, upsert_observation
from .base import BaseIngestor, utcnow

API = "https://api.dataplatform.knmi.nl/open-data/v1"
DATASET = "Actuele10mindataKNMIstations"
VERSION = "2"


class KnmiIngestor(BaseIngestor):
    name = "knmi"

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self.wanted: dict[str, list[tuple[str, str]]] = {}
        for key, city in cfg.cities.items():
            for fs in city.fast_sources:
                if fs["kind"] == "knmi":
                    self.wanted.setdefault(fs["station_code"], []).append((key, "resolution_fast"))
            for up in city.upstream:
                if up["kind"] == "knmi":
                    self.wanted.setdefault(up["id"], []).append((key, "upstream"))
        self._last_file: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.wanted) and bool(KNMI_API_KEY)

    async def poll(self) -> list[str]:
        if not self.wanted:
            return []
        if not KNMI_API_KEY:
            self.log_latency(None, ok=False, note="KNMI_API_KEY not set")
            return []
        hdrs = {"Authorization": KNMI_API_KEY}
        try:
            r = await self.get(
                f"{API}/datasets/{DATASET}/versions/{VERSION}/files",
                params={"maxKeys": 1, "orderBy": "created", "sorting": "desc"},
                headers=hdrs,
            )
            files = r.json().get("files", [])
            if not files:
                self.log_latency(None, ok=False, note="no files listed")
                return []
            fname = files[0]["filename"]
            if fname == self._last_file:
                return []  # nothing new
            r = await self.get(
                f"{API}/datasets/{DATASET}/versions/{VERSION}/files/{fname}/url",
                headers=hdrs,
            )
            url = r.json()["temporaryDownloadUrl"]
            blob = (await self.client.get(url, timeout=60)).content
        except Exception as e:
            self.log_latency(None, ok=False, note=f"{type(e).__name__}: {e}")
            return []

        try:
            rows, ts_obs = self._parse_nc(blob)
        except Exception as e:
            self.log_latency(None, ok=False, note=f"nc parse: {type(e).__name__}: {e}")
            return []

        changed: set[str] = set()
        with get_session() as s:
            for code, vals in rows.items():
                for city_key, _role in self.wanted.get(code, []):
                    if upsert_observation(
                        s, city=city_key, source="knmi", station=code,
                        ts_obs=ts_obs, ts_fetch=utcnow(), raw=fname[:120], **vals,
                    ):
                        changed.add(city_key)
            s.commit()
        self._last_file = fname
        self.log_latency(ts_obs, ok=True, note=fname)
        return sorted(changed)

    def _parse_nc(self, blob: bytes):
        import netCDF4
        with tempfile.NamedTemporaryFile(suffix=".nc") as tf:
            tf.write(blob)
            tf.flush()
            ds = netCDF4.Dataset(tf.name)
            try:
                stations = [
                    s if isinstance(s, str) else s.tobytes().decode().strip("\x00 ")
                    for s in netCDF4.chartostring(ds.variables["station"][:])
                ] if ds.variables["station"].dtype.kind in "SU" else [
                    str(x) for x in ds.variables["station"][:]
                ]
                time_var = ds.variables["time"]
                t0 = netCDF4.num2date(time_var[:][0], time_var.units)
                ts_obs = dt.datetime(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)

                def var(name):
                    if name in ds.variables:
                        v = ds.variables[name][:]
                        return np.ma.filled(v, np.nan).astype(float).ravel()
                    return None

                ta, td, rh = var("ta"), var("td"), var("rh")
                dd, ff = var("dd"), var("ff")
                rows: dict[str, dict] = {}
                for i, code in enumerate(stations):
                    norm = code.strip()
                    key = norm if norm in self.wanted else (
                        "06" + norm if ("06" + norm) in self.wanted else None)
                    if key is None:
                        continue
                    rows[key] = dict(
                        temp=_f(ta, i), dewp=_f(td, i), rh=_f(rh, i),
                        wdir=_f(dd, i), wspd=_f(ff, i),
                    )
                return rows, ts_obs
            finally:
                ds.close()


def _f(arr, i):
    if arr is None or i >= len(arr):
        return None
    v = float(arr[i])
    return None if np.isnan(v) else v
