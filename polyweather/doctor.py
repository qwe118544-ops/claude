"""`polyweather doctor` — validate every configured data source live.

Run this FIRST on the deployment machine. It checks connectivity, verifies
station IDs against official metadata (AMeDAS names -> IDs, KNMI codes in the
actual file, ICAO codes returning reports), confirms today's Polymarket event
slugs resolve, and reports the freshness of each source.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx

from .config import KNMI_API_KEY, USER_AGENT, get_config
from .ingest.amedas import BASE as AMEDAS_BASE
from .ingest.metar import API as METAR_API
from .ingest.openmeteo import API as OM_API, HOURLY, UPPER


def _ok(label: str, detail: str = ""):
    print(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))


def _fail(label: str, detail: str = ""):
    print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


async def check_metar(cfg, client) -> None:
    print("METAR (aviationweather.gov):")
    icaos = sorted({c.icao for c in cfg.cities.values()} |
                   {u["id"] for c in cfg.cities.values()
                    for u in c.upstream if u["kind"] == "metar"})
    try:
        r = await client.get(METAR_API, params={"ids": ",".join(icaos), "format": "json"})
        r.raise_for_status()
        reports = {x.get("icaoId"): x for x in r.json()}
        for icao in icaos:
            rep = reports.get(icao)
            if rep and rep.get("temp") is not None:
                _ok(icao, f"temp {rep['temp']}°C, raw: {(rep.get('rawOb') or '')[:60]}")
            elif rep:
                _fail(icao, "report present but no temp field")
            else:
                _fail(icao, "no report returned (station may be closed right now — "
                            "normal for EGLC overnight)")
    except Exception as e:
        _fail("endpoint", f"{type(e).__name__}: {e}")


async def check_amedas(cfg, client) -> None:
    print("AMeDAS (jma.go.jp):")
    names = sorted({fs["station_name"] for c in cfg.cities.values()
                    for fs in c.fast_sources if fs["kind"] == "amedas"} |
                   {u["id"] for c in cfg.cities.values()
                    for u in c.upstream if u["kind"] == "amedas"})
    if not names:
        return
    try:
        r = await client.get(f"{AMEDAS_BASE}/const/amedastable.json")
        r.raise_for_status()
        table = r.json()
        by_name = {}
        for sid, meta in table.items():
            by_name.setdefault(meta.get("kjName", ""), sid)
        r2 = await client.get(f"{AMEDAS_BASE}/data/latest_time.txt")
        latest = dt.datetime.fromisoformat(r2.text.strip())
        stamp = latest.strftime("%Y%m%d%H%M%S")
        r3 = await client.get(f"{AMEDAS_BASE}/data/map/{stamp}.json")
        data = r3.json()
        age = (dt.datetime.now(latest.tzinfo) - latest).total_seconds() / 60
        _ok("latest frame", f"{latest.isoformat()} ({age:.0f} min old)")
        for name in names:
            sid = by_name.get(name)
            if not sid:
                cands = [f"{v}:{k}" for k, v in by_name.items() if name[:1] in k][:5]
                _fail(f"station 「{name}」 not in amedastable.json",
                      f"candidates: {cands}")
                continue
            rec = data.get(sid, {})
            temp = rec.get("temp")
            if temp and temp[1] == 0:
                _ok(f"「{name}」 id={sid}", f"temp {temp[0]}°C")
            else:
                _fail(f"「{name}」 id={sid}", "no valid temp in latest frame")
    except Exception as e:
        _fail("endpoint", f"{type(e).__name__}: {e}")


async def check_knmi(cfg, client) -> None:
    print("KNMI (dataplatform.knmi.nl):")
    codes = sorted({fs["station_code"] for c in cfg.cities.values()
                    for fs in c.fast_sources if fs["kind"] == "knmi"} |
                   {u["id"] for c in cfg.cities.values()
                    for u in c.upstream if u["kind"] == "knmi"})
    if not codes:
        return
    if not KNMI_API_KEY:
        _fail("KNMI_API_KEY not set",
              "get a free key at https://developer.dataplatform.knmi.nl/ — "
              "Amsterdam falls back to METAR-only until then")
        return
    from .ingest.knmi import KnmiIngestor
    ing = KnmiIngestor(cfg, client=client)
    changed = await ing.poll()
    if changed:
        _ok("10-min file fetched+parsed", f"cities updated: {changed}")
    else:
        _fail("poll returned nothing", "check key validity / station codes; "
              "run again in 10 min (file may be unchanged)")


async def check_openmeteo(cfg, client) -> None:
    print("Open-Meteo:")
    for key, city in cfg.cities.items():
        try:
            r = await client.get(OM_API, params={
                "latitude": city.lat, "longitude": city.lon,
                "hourly": f"{HOURLY},{UPPER}", "models": ",".join(city.models),
                "timezone": city.tz, "forecast_days": 1,
            })
            if r.status_code != 200:
                _fail(key, f"HTTP {r.status_code}: {r.text[:120]}")
                continue
            hourly = r.json().get("hourly") or {}
            have = [m for m in city.models
                    if hourly.get(f"temperature_2m_{m}")
                    or (len(city.models) == 1 and hourly.get("temperature_2m"))]
            missing = set(city.models) - set(have)
            if missing:
                _fail(key, f"models with no data: {sorted(missing)} "
                           "(remove or rename them in cities.yaml)")
            else:
                _ok(key, f"{len(have)}/{len(city.models)} models return data")
        except Exception as e:
            _fail(key, f"{type(e).__name__}: {e}")


async def check_polymarket(cfg, client) -> None:
    print("Polymarket (gamma-api):")
    from .ingest.polymarket import PolymarketIngestor
    from .clock import local_now
    ing = PolymarketIngestor(cfg, client=client)
    for key, city in cfg.cities.items():
        found = None
        for cand in ing._slug_candidates(city, local_now(city).date()):
            try:
                ev = await ing._fetch_event(cand)
            except Exception as e:
                _fail(key, f"{type(e).__name__}: {e}")
                break
            if ev:
                found = (cand, ev)
                break
        if found:
            slug, ev = found
            n = len(ev.get("markets", []))
            _ok(key, f"{slug} ({n} buckets)")
        else:
            _fail(key, "no event found for today — check slug pattern on "
                       "polymarket.com and adjust market_city_slug")


async def run() -> None:
    cfg = get_config()
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        await check_metar(cfg, client)
        await check_amedas(cfg, client)
        await check_knmi(cfg, client)
        await check_openmeteo(cfg, client)
        await check_polymarket(cfg, client)
    print("\nDoctor finished. Fix any [FAIL] lines before trusting prices.")
