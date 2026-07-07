"""Breeze-front triad detector on synthetic Amsterdam data."""
import datetime as dt

from polyweather.config import get_config
from polyweather.db import Observation, get_session
from polyweather.ingest.base import wind_dir_in_sector
from polyweather.signals import breeze_front

CFG = get_config()
NOW = dt.datetime(2026, 7, 7, 11, 0)  # naive UTC = 13:00 CEST


def _add(s, station, minutes_ago, temp, dewp, wdir):
    s.add(Observation(
        city="amsterdam", source="knmi", station=station,
        ts_obs=NOW - dt.timedelta(minutes=minutes_ago), ts_fetch=NOW,
        temp=temp, dewp=dewp, wdir=wdir, wspd=4.0, rh=None, raw=None))


def test_sector_wrap():
    assert wind_dir_in_sector(300, [250, 360])
    assert not wind_dir_in_sector(180, [250, 360])
    assert wind_dir_in_sector(10, [350, 30])   # wrapping sector
    assert not wind_dir_in_sector(180, [350, 30])


def test_front_detected_at_coast():
    with get_session() as s:
        s.query(Observation).filter_by(city="amsterdam").delete()
        # IJmuiden: offshore easterly, then veer to onshore W with dewpoint jump + cooling
        for m, t, td, wd in [(90, 24.0, 10.0, 120), (80, 24.5, 10.0, 110),
                             (70, 25.0, 10.2, 100), (60, 25.2, 10.1, 130),
                             (50, 24.8, 12.5, 280), (40, 24.0, 13.0, 290),
                             (30, 23.4, 13.2, 300), (20, 23.0, 13.4, 300),
                             (10, 22.8, 13.5, 310)]:
            _add(s, "06225", m, t, td, wd)
        # Schiphol itself: still offshore flow, still warming
        for m, t, td, wd in [(90, 25.0, 10.0, 120), (60, 25.8, 10.0, 110),
                             (30, 26.4, 10.2, 100), (10, 26.8, 10.1, 110)]:
            _add(s, "06240", m, t, td, wd)
            _add(s, "EHAM", m, t, td, wd)
        s.commit()

    res = breeze_front.evaluate(CFG, "amsterdam", NOW)
    assert res["sentinels"]["IJmuiden"]["triggered"] is True
    assert res["confidence"] > 0.2
    assert res["arrived"] is False
    assert res["eta_min"] is not None
    # front hit the coast ~50 min ago, travel 75 min -> ETA ~+25 min
    assert 0 < res["eta_min"] < 60


def test_no_front_when_flow_stays_offshore():
    with get_session() as s:
        s.query(Observation).filter_by(city="amsterdam").delete()
        for m, t in [(90, 24.0), (60, 25.0), (30, 25.8), (10, 26.2)]:
            _add(s, "06225", m, t, 10.0, 110)
            _add(s, "EHAM", m, t, 10.0, 115)
        s.commit()
    res = breeze_front.evaluate(CFG, "amsterdam", NOW)
    assert res["confidence"] == 0.0
    assert res["arrived"] is False
