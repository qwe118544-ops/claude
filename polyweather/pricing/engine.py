"""Pricing engine: one recompute produces the settlement-integer PMF for a
city's climate day, with every signal's contribution recorded.

Pipeline per recompute:
  1. observations -> run_max (integer settlement floor + continuous estimate)
  2. model mixture over 'max of remaining hours' (+ per-model bias/sigma,
     momentum shift, residual shift/widen)
  3. analog kNN empirical residual distribution blended in
  4. thermodynamic ceiling soft caps
  5. breeze-front cap (arrived, or ETA inside horizon)
  6. peak-passed exceedance scaling
  7. floor at running max -> integer PMF -> persist PricingVersion
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np

from ..clock import (climate_date, climate_day_bounds_utc, day_phase,
                     hours_left_in_warming_window, local_now)
from ..config import DATA_DIR
from ..db import Observation, ModelForecast, PricingVersion, get_session
from ..ingest.base import utcnow
from ..signals import breeze_front, ceiling, momentum, peak_passed, residual
from .analog import get_analog
from .dist import TempDist


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        g = cfg.grid
        self.grid = TempDist.make_grid(g["t_min"], g["t_max"], g["step"])
        self._model_stats = self._load_model_stats()

    def _load_model_stats(self) -> dict:
        path = DATA_DIR / "climatology" / "model_stats.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {}

    # ---------- input gathering ----------
    def _sigma_for(self, city_key: str, model: str, hours_left: float) -> float:
        stats = self._model_stats.get(city_key, {}).get(model)
        table = (stats or {}).get("sigma_by_hours_left") \
            or self.cfg.engine["default_sigma_by_hours_left"]
        pts = sorted((float(k), float(v)) for k, v in table.items())
        h = min(max(hours_left, pts[0][0]), pts[-1][0])
        for (h0, s0), (h1, s1) in zip(pts, pts[1:]):
            if h0 <= h <= h1:
                return s0 + (s1 - s0) * (h - h0) / (h1 - h0 or 1)
        return pts[-1][1]

    def _bias_for(self, city_key: str, model: str) -> float:
        stats = self._model_stats.get(city_key, {}).get(model)
        return float((stats or {}).get("bias", self.cfg.engine["default_model_bias_c"]))

    def _observations(self, s, city_key: str, cdate: str):
        city = self.cfg.cities[city_key]
        start, end = climate_day_bounds_utc(city, cdate)
        start_n, end_n = start.replace(tzinfo=None), end.replace(tzinfo=None)
        rows = (
            s.query(Observation)
            .filter(Observation.city == city_key,
                    Observation.ts_obs >= start_n, Observation.ts_obs < end_n,
                    Observation.temp.isnot(None))
            .order_by(Observation.ts_obs)
            .all()
        )
        res = [r for r in rows if r.station == city.icao]
        fast_codes = {fs.get("station_code") for fs in city.fast_sources}
        fast = [r for r in rows if r.station in fast_codes or
                (r.source == "amedas" and any(
                    r.raw == fs.get("station_name") for fs in city.fast_sources))]
        return res, fast

    def _future_max_per_model(self, s, city_key: str, cdate: str,
                              now_local: dt.datetime) -> dict[str, float]:
        city = self.cfg.cities[city_key]
        rows = (
            s.query(ModelForecast)
            .filter(ModelForecast.city == city_key,
                    ModelForecast.climate_date == cdate)
            .order_by(ModelForecast.ts_fetch.desc())
            .limit(len(city.models) * 2)
            .all()
        )
        seen: set[str] = set()
        out: dict[str, float] = {}
        now_key = now_local.strftime("%Y-%m-%dT%H:%M")
        for r in rows:
            if r.model in seen:
                continue
            seen.add(r.model)
            t = r.hourly.get("time") or []
            v = r.hourly.get("temperature_2m") or []
            fut = [val for ts, val in zip(t, v)
                   if ts.startswith(cdate) and ts >= now_key[:13] and val is not None]
            if fut:
                out[r.model] = max(fut)
        return out

    # ---------- the recompute ----------
    def recompute(self, city_key: str, now: dt.datetime | None = None) -> dict | None:
        now = now or utcnow()
        cfg_e = self.cfg.engine
        city = self.cfg.cities[city_key]
        cdate = climate_date(city, now.replace(tzinfo=dt.timezone.utc))
        loc = local_now(city, now.replace(tzinfo=dt.timezone.utc))
        phase = day_phase(city, self.cfg.summer_months, now.replace(tzinfo=dt.timezone.utc))
        hours_left = hours_left_in_warming_window(
            city, self.cfg.summer_months, now.replace(tzinfo=dt.timezone.utc))

        with get_session() as s:
            res_obs, fast_obs = self._observations(s, city_key, cdate)
            future_max = self._future_max_per_model(s, city_key, cdate, loc)

        if not res_obs and not future_max:
            return None  # nothing to price yet

        # 1. running max
        run_max_int = max((int(round(r.temp)) for r in res_obs), default=None)
        cont_candidates = [r.temp for r in res_obs] + [r.temp for r in fast_obs]
        run_max_cont = max(cont_candidates, default=None)
        last_temp = res_obs[-1].temp if res_obs else (
            fast_obs[-1].temp if fast_obs else None)

        # signals
        sig_breeze = breeze_front.evaluate(self.cfg, city_key, now)
        sig_ceiling = ceiling.evaluate(self.cfg, city_key, cdate, now)
        sig_resid = residual.evaluate(self.cfg, city_key, cdate, now)
        sig_moment = momentum.evaluate(self.cfg, city_key, cdate)
        sig_peak = peak_passed.evaluate(self.cfg, city_key, now, sig_breeze)

        # 2. model mixture on future max
        means, sigmas, weights, used_models = [], [], [], []
        resid_shift = cfg_e["residual_gain"] * sig_resid["mean_residual"] \
            if sig_resid["n_obs"] >= 2 else 0.0
        resid_shift = float(np.clip(resid_shift, -1.5, 1.5))
        mom_shift = float(np.clip(cfg_e["momentum_gain"] * sig_moment["overall"], -1.0, 1.0))
        for model, fmax in future_max.items():
            means.append(fmax + self._bias_for(city_key, model) + resid_shift + mom_shift)
            sigmas.append(self._sigma_for(city_key, model, hours_left))
            weights.append(1.0)
            used_models.append(model)

        if means:
            dist = TempDist.from_gaussian_mixture(self.grid, means, sigmas, weights)
        elif run_max_cont is not None:
            dist = TempDist.from_gaussian_mixture(
                self.grid, [run_max_cont], [max(0.4, hours_left * 0.15)], [1.0])
        else:
            return None

        # residual trend breaks the script -> widen
        widen = 1.0 + cfg_e["residual_widen_gain"] * min(abs(sig_resid["trend"]), 2.0)
        if widen > 1.02:
            dist = dist.widen(widen)

        # 3. analog blend
        analog = get_analog(city_key)
        analog_used = False
        if analog.available and run_max_cont is not None and last_temp is not None:
            slot = loc.hour * 2 + (1 if loc.minute >= 30 else 0)
            slope = self._slope_3h(res_obs or fast_obs)
            samples = analog.residual_samples(
                doy=loc.timetuple().tm_yday, slot=slot, temp_now=last_temp,
                run_max=run_max_cont, slope_3h=slope, k=int(cfg_e["analog_k"]))
            if len(samples) >= 15:
                a_dist = TempDist.from_samples(self.grid, run_max_cont + samples)
                w = cfg_e["analog_weight_peak"] if phase == "PEAK" \
                    else cfg_e["analog_weight_other"]
                dist = dist.blend(a_dist, w)
                analog_used = True

        # 4. ceiling caps
        margin = cfg_e["ceiling_margin_c"]
        if sig_ceiling["cap_850"] is not None:
            dist = dist.soft_cap(sig_ceiling["cap_850"] + margin, strength=0.9)
        if sig_ceiling["cap_925"] is not None and loc.hour < 12:
            dist = dist.soft_cap(sig_ceiling["cap_925"] + margin, strength=0.5)

        # 5. breeze front
        breeze_applied = False
        eta = sig_breeze.get("eta_min")
        if sig_breeze["arrived"] or (
                sig_breeze["confidence"] >= 0.5 and eta is not None
                and eta <= cfg_e["breeze_eta_horizon_min"]):
            base = max(x for x in (run_max_cont, last_temp) if x is not None) \
                if (run_max_cont is not None or last_temp is not None) else None
            if base is not None:
                cap = base + city.breeze["post_front_rise_c"]
                # pre-arrival: allow the remaining minutes of warming
                if not sig_breeze["arrived"] and eta and eta > 0:
                    cap += 0.015 * eta  # ~0.9 °C/h typical late-morning rate
                dist = dist.soft_cap(cap, strength=max(sig_breeze["confidence"], 0.6))
                breeze_applied = True

        # 6. peak passed
        anchor = run_max_cont if run_max_cont is not None else dist.mean()
        dist = dist.scale_exceedance(anchor, 1.0 - sig_peak["p"])

        # 7. floor at running max, integer PMF
        if run_max_cont is not None:
            dist = dist.floor_at(run_max_cont)
        int_pmf = dist.integer_pmf(run_max_int=run_max_int,
                                   sampling_deficit=city.sampling_deficit)

        signals = {
            "breeze_front": sig_breeze, "ceiling": sig_ceiling,
            "residual": sig_resid, "momentum": sig_moment,
            "peak_passed": sig_peak,
            "applied": {"breeze_cap": breeze_applied, "analog": analog_used,
                        "resid_shift": round(resid_shift, 2),
                        "momentum_shift": round(mom_shift, 2)},
        }
        inputs = {
            "n_res_obs": len(res_obs), "n_fast_obs": len(fast_obs),
            "run_max_int": run_max_int,
            "run_max_cont": round(run_max_cont, 1) if run_max_cont is not None else None,
            "last_temp": round(last_temp, 1) if last_temp is not None else None,
            "future_max_per_model": {k: round(v, 1) for k, v in future_max.items()},
            "hours_left": round(hours_left, 2), "local_time": loc.strftime("%H:%M"),
        }

        with get_session() as s:
            prev = (
                s.query(PricingVersion)
                .filter(PricingVersion.city == city_key,
                        PricingVersion.climate_date == cdate)
                .order_by(PricingVersion.ts.desc())
                .first()
            )
            explain = self._explain(prev, int_pmf, signals, inputs)
            pv = PricingVersion(
                city=city_key, climate_date=cdate, ts=now, phase=phase,
                run_max_int=run_max_int,
                int_pmf={str(k): round(v, 5) for k, v in int_pmf.items()},
                inputs=inputs, signals=signals, explain=explain,
            )
            s.add(pv)
            s.commit()
            out = {"id": pv.id, "city": city_key, "climate_date": cdate,
                   "ts": now.isoformat(), "phase": phase,
                   "run_max_int": run_max_int, "int_pmf": pv.int_pmf,
                   "signals": signals, "inputs": inputs, "explain": explain}
        return out

    @staticmethod
    def _slope_3h(obs) -> float:
        if not obs:
            return 0.0
        latest = obs[-1]
        target = latest.ts_obs - dt.timedelta(hours=3)
        past = min(obs, key=lambda r: abs((r.ts_obs - target).total_seconds()))
        if past.ts_obs == latest.ts_obs:
            return 0.0
        return latest.temp - past.temp

    @staticmethod
    def _explain(prev, int_pmf: dict, signals: dict, inputs: dict) -> list[str]:
        lines = []
        if prev is not None:
            old = {int(k): v for k, v in prev.int_pmf.items()}
            moves = []
            for j in sorted(set(old) | set(int_pmf)):
                d = int_pmf.get(j, 0.0) - old.get(j, 0.0)
                if abs(d) >= 0.03:
                    moves.append((abs(d), f"{j}°C {old.get(j, 0):.0%}→{int_pmf.get(j, 0):.0%}"))
            for _, m in sorted(moves, reverse=True)[:3]:
                lines.append(m)
            if prev.run_max_int != inputs["run_max_int"]:
                lines.append(f"running max {prev.run_max_int}→{inputs['run_max_int']}°C")
        b = signals["breeze_front"]
        if b["arrived"]:
            lines.append("breeze front ARRIVED at station")
        elif b["confidence"] >= 0.5:
            lines.append(f"breeze front inbound, conf {b['confidence']:.0%}, ETA {b['eta_min']}min")
        p = signals["peak_passed"]["p"]
        if p >= 0.8:
            lines.append(f"P(peak already passed)={p:.0%}")
        r = signals["residual"]
        if abs(r["mean_residual"]) >= 0.7:
            lines.append(f"obs running {r['mean_residual']:+.1f}°C vs models")
        return lines
