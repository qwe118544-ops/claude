"""FastAPI app: read-only REST + SSE, serves the static UI."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..bus import BUS
from ..clock import climate_date, climate_day_bounds_utc, day_phase, local_now
from ..config import ROOT, get_config
from ..db import (DailyOutcome, LatencyLog, MarketSnapshot, Observation,
                  ModelForecast, PricingVersion, SignalEvent, get_session, init_db)
from ..pricing.buckets import bucket_prob
from ..scheduler import Runtime

runtime: Runtime | None = None


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global runtime
    init_db()
    runtime = Runtime()
    runtime.start()
    try:
        yield
    finally:
        if runtime:
            await runtime.stop()


app = FastAPI(title="polyweather", lifespan=_lifespan)


@app.get("/api/cities")
def cities():
    cfg = get_config()
    out = []
    for key, c in cfg.cities.items():
        out.append({
            "key": key, "name": c.name, "icao": c.icao, "tz": c.tz,
            "local_time": local_now(c).strftime("%H:%M"),
            "phase": day_phase(c, cfg.summer_months),
            "climate_date": climate_date(c),
        })
    return out


@app.get("/api/state/{city_key}")
def state(city_key: str):
    """Everything the UI needs for one city, in one call."""
    cfg = get_config()
    if city_key not in cfg.cities:
        raise HTTPException(404)
    city = cfg.cities[city_key]
    cdate = climate_date(city)
    start, end = climate_day_bounds_utc(city, cdate)
    start_n, end_n = start.replace(tzinfo=None), end.replace(tzinfo=None)

    with get_session() as s:
        pv = (s.query(PricingVersion)
              .filter_by(city=city_key, climate_date=cdate)
              .order_by(PricingVersion.ts.desc()).first())
        obs = (s.query(Observation)
               .filter(Observation.city == city_key,
                       Observation.station == city.icao,
                       Observation.ts_obs >= start_n, Observation.ts_obs < end_n,
                       Observation.temp.isnot(None))
               .order_by(Observation.ts_obs).all())
        fast_codes = {fs.get("station_code") for fs in city.fast_sources} - {None}
        fast = (s.query(Observation)
                .filter(Observation.city == city_key,
                        Observation.source.in_(("knmi", "amedas")),
                        Observation.ts_obs >= start_n, Observation.ts_obs < end_n,
                        Observation.temp.isnot(None))
                .order_by(Observation.ts_obs).all())
        fast = [o for o in fast
                if o.station in fast_codes
                or any(o.raw == fs.get("station_name") for fs in city.fast_sources)]
        mkt = (s.query(MarketSnapshot)
               .filter_by(city=city_key, climate_date=cdate)
               .order_by(MarketSnapshot.ts.desc()).first())
        sigs = (s.query(SignalEvent)
                .filter(SignalEvent.city == city_key, SignalEvent.ts >= start_n)
                .order_by(SignalEvent.ts.desc()).limit(20).all())
        versions = (s.query(PricingVersion.ts, PricingVersion.explain)
                    .filter_by(city=city_key, climate_date=cdate)
                    .order_by(PricingVersion.ts.desc()).limit(30).all())

        # model curves (latest per model)
        rows = (s.query(ModelForecast)
                .filter_by(city=city_key, climate_date=cdate)
                .order_by(ModelForecast.ts_fetch.desc())
                .limit(len(city.models) * 2).all())
        seen, curves = set(), {}
        for r in rows:
            if r.model in seen:
                continue
            seen.add(r.model)
            t = r.hourly.get("time") or []
            v = r.hourly.get("temperature_2m") or []
            curves[r.model] = [[ts, val] for ts, val in zip(t, v)
                               if ts.startswith(cdate) and val is not None]

    market_buckets = []
    if mkt and pv:
        pmf = {int(k): float(p) for k, p in pv.int_pmf.items()}
        for b in mkt.buckets:
            ours = bucket_prob(pmf, b["lo"], b["hi"]) \
                if b.get("lo") is not None else None
            market_buckets.append({**b, "ours": round(ours, 3) if ours is not None else None})
    elif mkt:
        market_buckets = mkt.buckets

    return {
        "city": city_key, "name": city.name, "climate_date": cdate,
        "local_time": local_now(city).strftime("%H:%M"),
        "phase": day_phase(city, get_config().summer_months),
        "pricing": None if pv is None else {
            "ts": pv.ts.isoformat(), "int_pmf": pv.int_pmf,
            "run_max_int": pv.run_max_int, "signals": pv.signals,
            "inputs": pv.inputs, "explain": pv.explain, "phase": pv.phase,
        },
        "obs": [[o.ts_obs.isoformat(), o.temp] for o in obs],
        "fast_obs": [[o.ts_obs.isoformat(), o.temp] for o in fast],
        "model_curves": curves,
        "market": {"event_slug": mkt.event_slug if mkt else None,
                   "ts": mkt.ts.isoformat() if mkt else None,
                   "buckets": market_buckets},
        "signal_events": [
            {"ts": e.ts.isoformat(), "kind": e.kind, "active": e.active,
             "payload": e.payload} for e in sigs],
        "explain_feed": [
            {"ts": ts.isoformat(), "lines": ex} for ts, ex in versions if ex],
    }


@app.get("/api/health")
def health():
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=6)
    with get_session() as s:
        rows = (s.query(LatencyLog)
                .filter(LatencyLog.ts_fetch >= cutoff)
                .order_by(LatencyLog.ts_fetch.desc()).limit(500).all())
    per: dict[str, dict] = {}
    for r in rows:
        d = per.setdefault(r.source, {"last_fetch": r.ts_fetch.isoformat() + "Z",
                                      "last_ok": None, "last_lag_s": None,
                                      "errors_6h": 0})
        if r.ok and d["last_ok"] is None:
            d["last_ok"] = r.ts_fetch.isoformat() + "Z"
            d["last_lag_s"] = r.lag_s
        if not r.ok:
            d["errors_6h"] += 1
    return per


@app.get("/api/eval")
def eval_summary(days: int = 30):
    with get_session() as s:
        rows = (s.query(DailyOutcome)
                .order_by(DailyOutcome.climate_date.desc())
                .limit(days * 3).all())
    return [{
        "city": r.city, "date": r.climate_date, "settle": r.settle_int,
        "peak_local": r.peak_time_local, "scores": r.scores,
    } for r in rows]


@app.get("/stream")
async def stream():
    q = BUS.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            BUS.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
