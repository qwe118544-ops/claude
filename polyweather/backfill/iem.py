"""Backfill from the Iowa Environmental Mesonet ASOS/METAR archive (free).

Builds, per city:
  data/climatology/{city}_days.parquet     - daily half-hour trajectory matrix
      columns: date, doy, final_max, peak_slot, t0..t47 (temp at local
      half-hour slot), rmax0..rmax47 (running max at slot)
  data/climatology/{city}_peaktime.json    - per month, cumulative fraction of
      days whose peak occurred at or before each half-hour slot

These power the analog kNN model and the peak-passed climatology.
"""
from __future__ import annotations

import datetime as dt
import io
import json
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

from ..config import DATA_DIR, USER_AGENT

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch_station_history(icao: str, year_from: int, year_to: int) -> pd.DataFrame:
    """Download tmpc series (UTC) for one station, chunked by year."""
    frames = []
    with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as client:
        for year in range(year_from, year_to + 1):
            params = {
                "station": icao, "data": "tmpc",
                "year1": year, "month1": 1, "day1": 1,
                "year2": year, "month2": 12, "day2": 31,
                "tz": "Etc/UTC", "format": "onlycomma",
                "latlon": "no", "missing": "empty", "trace": "empty",
            }
            r = client.get(IEM, params=params)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            if not df.empty:
                frames.append(df)
            print(f"  {icao} {year}: {len(df)} rows")
    if not frames:
        return pd.DataFrame(columns=["station", "valid", "tmpc"])
    out = pd.concat(frames, ignore_index=True)
    out["valid"] = pd.to_datetime(out["valid"], utc=True)
    out["tmpc"] = pd.to_numeric(out["tmpc"], errors="coerce")
    return out.dropna(subset=["tmpc"])


def build_city(city_key: str, city, years: int = 12) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    df = fetch_station_history(city.icao, now.year - years, now.year)
    if df.empty:
        raise RuntimeError(f"IEM returned no data for {city.icao}")
    tz = ZoneInfo(city.tz)
    df["local"] = df["valid"].dt.tz_convert(tz)
    df["date"] = df["local"].dt.date
    df["slot"] = df["local"].dt.hour * 2 + (df["local"].dt.minute >= 30).astype(int)

    rows = []
    for date, day in df.groupby("date"):
        # per-slot temperature (max within slot), require reasonable coverage
        slot_temp = day.groupby("slot")["tmpc"].max()
        if len(slot_temp) < 20:
            continue
        final_max = float(day["tmpc"].max())
        peak_slot = int(day.loc[day["tmpc"].idxmax(), "slot"])
        rec: dict = {
            "date": str(date), "doy": pd.Timestamp(date).dayofyear,
            "month": pd.Timestamp(date).month,
            "final_max": final_max, "peak_slot": peak_slot,
        }
        run = -99.0
        for slot in range(48):
            v = slot_temp.get(slot, np.nan)
            rec[f"t{slot}"] = v
            if not np.isnan(v):
                run = max(run, float(v))
            rec[f"rmax{slot}"] = run if run > -90 else np.nan
        rows.append(rec)
    days = pd.DataFrame(rows)

    out_dir = DATA_DIR / "climatology"
    out_dir.mkdir(parents=True, exist_ok=True)
    days.to_parquet(out_dir / f"{city_key}_days.parquet", index=False)

    # peak-time cumulative distribution per month
    peaktime: dict[str, list[float]] = {}
    for month, grp in days.groupby("month"):
        cum = [float((grp["peak_slot"] <= slot).mean()) for slot in range(48)]
        peaktime[str(int(month))] = [round(x, 4) for x in cum]
    with open(out_dir / f"{city_key}_peaktime.json", "w") as f:
        json.dump(peaktime, f)
    print(f"{city_key}: {len(days)} days -> {out_dir}")


def run(cfg, years: int = 12, only_city: str | None = None) -> None:
    for key, city in cfg.cities.items():
        if only_city and key != only_city:
            continue
        print(f"== backfilling {key} ({city.icao}) from IEM, {years}y ==")
        build_city(key, city, years=years)
