"""End-to-end engine test on synthetic Tokyo data (no network)."""
import datetime as dt

from polyweather.config import get_config
from polyweather.db import ModelForecast, Observation, PricingVersion, get_session
from polyweather.pricing.engine import Engine

CFG = get_config()
# 12:30 JST on 2026-07-07 = 03:30 UTC
NOW = dt.datetime(2026, 7, 7, 3, 30)
CDATE = "2026-07-07"


def _seed(breeze_arrived: bool):
    with get_session() as s:
        for table, city in ((Observation, "tokyo"), (ModelForecast, "tokyo"),
                            (PricingVersion, "tokyo")):
            s.query(table).filter_by(city=city).delete()
        # RJTT METARs: warming morning, 31.2 max so far
        seq = [(300, 27.0, 21.0, 350), (240, 28.4, 21.0, 340),
               (180, 29.6, 21.0, 330), (120, 30.5, 21.5, 320),
               (60, 31.2, 21.5, 330)]
        if breeze_arrived:
            # last two obs: wind swings onshore (S), dewpoint jumps, temp off max
            seq += [(30, 30.6, 24.0, 180), (5, 30.2, 24.3, 190)]
        else:
            seq += [(30, 31.4, 21.4, 330), (5, 31.6, 21.3, 335)]
        for m_ago, t, td, wd in seq:
            s.add(Observation(city="tokyo", source="metar", station="RJTT",
                              ts_obs=NOW - dt.timedelta(minutes=m_ago),
                              ts_fetch=NOW, temp=t, dewp=td, wdir=wd,
                              wspd=4.0, rh=None, raw=None))
        # two models predicting a 33-34 °C afternoon
        times = [f"{CDATE}T{h:02d}:00" for h in range(24)]
        curve = [26, 25.5, 25, 25, 25.5, 26, 27, 28, 29, 30, 31, 31.8,
                 32.4, 33.4, 33.0, 32.2, 31.0, 30.0, 29.0, 28.2, 27.6, 27, 26.6, 26.2]
        for model, off in (("ecmwf_ifs025", 0.0), ("jma_msm", 0.6)):
            s.add(ModelForecast(
                city="tokyo", model=model, ts_fetch=NOW - dt.timedelta(minutes=40),
                climate_date=CDATE, today_max=max(curve) + off,
                hourly={"time": times,
                        "temperature_2m": [c + off for c in curve]},
                upper=None))
        s.commit()


def test_engine_prices_normal_day():
    _seed(breeze_arrived=False)
    out = Engine(CFG).recompute("tokyo", NOW)
    assert out is not None
    pmf = {int(k): v for k, v in out["int_pmf"].items()}
    assert abs(sum(pmf.values()) - 1.0) < 1e-4
    assert out["run_max_int"] == 32  # last obs 31.6 -> int 32
    assert min(pmf) >= 32           # settlement can't be below running max
    # models say ~33.4-34: bulk of mass should be 33-34
    assert pmf.get(33, 0) + pmf.get(34, 0) > 0.4


def test_breeze_arrival_caps_distribution():
    _seed(breeze_arrived=False)
    e = Engine(CFG)
    out_open = e.recompute("tokyo", NOW)
    _seed(breeze_arrived=True)
    out_capped = e.recompute("tokyo", NOW)

    def p_ge(out, j):
        return sum(v for k, v in out["int_pmf"].items() if int(k) >= j)

    assert out_capped["signals"]["breeze_front"]["arrived"] is True
    # probability of reaching 33+ collapses after the front arrives
    assert p_ge(out_capped, 33) < p_ge(out_open, 33) * 0.55
    # and peak-passed pushes mass onto the realized max (31 = int(30.6..31.2))
    assert out_capped["run_max_int"] == 31


def test_pricing_version_persisted_with_snapshot():
    _seed(breeze_arrived=False)
    Engine(CFG).recompute("tokyo", NOW)
    with get_session() as s:
        pv = (s.query(PricingVersion).filter_by(city="tokyo", climate_date=CDATE)
              .order_by(PricingVersion.ts.desc()).first())
    assert pv is not None
    assert pv.inputs["n_res_obs"] >= 6
    assert "ecmwf_ifs025" in pv.inputs["future_max_per_model"]
    assert pv.signals["peak_passed"]["p"] >= 0.0
