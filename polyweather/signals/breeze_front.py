"""Sea/estuary-breeze front detector — the #1 cause of 'unexpected' capped
highs at all three airports (EGLC on the Thames, EHAM 25 km from the North
Sea, RJTT on Tokyo Bay).

Triad rule per sentinel station, over its last 90 minutes of observations:
  1. wind veers INTO the configured onshore sector and stays for >=2 readings
  2. dewpoint jumps  (>= +1.0 °C vs the pre-shift level)   [marine air]
  3. temperature drops (>= 0.8 °C off its running max)
Two of the three (wind shift mandatory) = sentinel triggered.

Front state per city:
  confidence = weighted fraction of triggered sentinels
  eta_min    = weighted mean of (trigger_time + travel_min) - now
  arrived    = triad true at the resolution station itself
"""
from __future__ import annotations

import datetime as dt

from ..db import Observation, SignalEvent, get_session
from ..ingest.base import utcnow, wind_dir_in_sector

LOOKBACK_MIN = 100
MIN_SUSTAINED = 2
DEWP_JUMP = 1.0
TEMP_DROP = 0.8


def _station_series(s, city_key: str, station: str, now: dt.datetime):
    since = now - dt.timedelta(minutes=LOOKBACK_MIN)
    rows = (
        s.query(Observation)
        .filter(Observation.city == city_key, Observation.station == station,
                Observation.ts_obs >= since)
        .order_by(Observation.ts_obs)
        .all()
    )
    return rows


def _triad(rows, sector) -> tuple[bool, dt.datetime | None, dict]:
    """Evaluate the triad on one station's recent series."""
    if len(rows) < 3:
        return False, None, {}
    onshore = [wind_dir_in_sector(r.wdir, sector) for r in rows]
    # find first index where onshore becomes true and stays true
    shift_i = None
    for i in range(1, len(rows)):
        if not onshore[i - 1] and all(onshore[i:i + MIN_SUSTAINED]) \
                and len(onshore[i:i + MIN_SUSTAINED]) >= MIN_SUSTAINED:
            shift_i = i
            break
    if shift_i is None:
        # already fully onshore through the window: treat as shifted at start
        if all(onshore) and len(rows) >= MIN_SUSTAINED:
            shift_i = 0
        else:
            return False, None, {}
    pre = rows[:shift_i] or rows[:1]
    post = rows[shift_i:]
    pre_dewp = [r.dewp for r in pre if r.dewp is not None]
    post_dewp = [r.dewp for r in post if r.dewp is not None]
    dewp_jump = (max(post_dewp) - (sum(pre_dewp) / len(pre_dewp))) >= DEWP_JUMP \
        if pre_dewp and post_dewp else False
    temps = [r.temp for r in rows if r.temp is not None]
    post_temps = [r.temp for r in post if r.temp is not None]
    temp_drop = (max(temps) - post_temps[-1]) >= TEMP_DROP if temps and post_temps else False
    ok = dewp_jump or temp_drop  # wind shift is mandatory and already established
    detail = {"dewp_jump": dewp_jump, "temp_drop": temp_drop,
              "shift_time": rows[shift_i].ts_obs.isoformat()}
    return ok, rows[shift_i].ts_obs, detail


def evaluate(cfg, city_key: str, now: dt.datetime | None = None) -> dict:
    """Returns {'confidence': float, 'eta_min': float|None, 'arrived': bool,
    'sentinels': {...}} and persists a SignalEvent on state change."""
    now = now or utcnow()
    city = cfg.cities[city_key]
    sector = city.breeze["onshore_sector"]
    result = {"confidence": 0.0, "eta_min": None, "arrived": False, "sentinels": {}}

    with get_session() as s:
        # sentinels
        total_w = triggered_w = 0.0
        etas = []
        for up in city.upstream:
            w = float(up.get("weight", 1.0))
            travel = float(up.get("travel_min", 60))
            if travel < 0:
                continue  # reference stations don't vote
            total_w += w
            sid = up["id"] if up["kind"] != "amedas" else _amedas_id(s, city_key, up["id"])
            if sid is None:
                continue
            rows = _station_series(s, city_key, sid, now)
            ok, t_shift, detail = _triad(rows, sector)
            result["sentinels"][up["name"]] = {"triggered": ok, **detail}
            if ok and t_shift is not None:
                triggered_w += w
                eta = (t_shift + dt.timedelta(minutes=travel) - now).total_seconds() / 60
                etas.append((eta, w))
        if total_w > 0:
            result["confidence"] = round(triggered_w / total_w, 3)
        if etas:
            result["eta_min"] = round(sum(e * w for e, w in etas) / sum(w for _, w in etas), 1)

        # resolution station itself (METAR + fast source share the city rows;
        # use the resolution ICAO series for arrival)
        rows = _station_series(s, city_key, city.icao, now)
        arrived, _, detail = _triad(rows, sector)
        # also accept arrival via the fast source (10-min data sees it earlier)
        for fs in city.fast_sources:
            if arrived:
                break
            sid = fs.get("station_code") or _amedas_id(s, city_key, fs.get("station_name", ""))
            if sid:
                rows_f = _station_series(s, city_key, sid, now)
                arrived, _, detail = _triad(rows_f, sector)
        result["arrived"] = bool(arrived)
        if arrived:
            result["confidence"] = max(result["confidence"], 0.95)
            result["eta_min"] = 0.0

        _persist_on_change(s, city_key, "breeze_front", result, now)
        s.commit()
    return result


def _amedas_id(s, city_key: str, name: str) -> str | None:
    """AMeDAS rows store the kanji name in `raw`; find the numeric station id."""
    row = (
        s.query(Observation)
        .filter(Observation.city == city_key, Observation.source == "amedas",
                Observation.raw == name)
        .order_by(Observation.ts_obs.desc())
        .first()
    )
    return row.station if row else None


def _persist_on_change(s, city_key: str, kind: str, payload: dict, now: dt.datetime):
    last = (
        s.query(SignalEvent)
        .filter(SignalEvent.city == city_key, SignalEvent.kind == kind)
        .order_by(SignalEvent.ts.desc())
        .first()
    )
    active = payload.get("arrived") or payload.get("confidence", 0) >= 0.5
    was_active = bool(last and last.active)
    if active != was_active or last is None:
        s.add(SignalEvent(city=city_key, ts=now, kind=kind, active=active, payload=payload))
