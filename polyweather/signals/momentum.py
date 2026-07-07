"""Model momentum: run-to-run revision of each model's 'today max'.

We store every Open-Meteo fetch; consecutive fetches that change today_max
represent new model runs. A consistent revision direction across recent
revisions means the models are chasing something they missed — shift our
means along it.
"""
from __future__ import annotations

from ..db import ModelForecast, get_session

MAX_FETCHES = 12  # look back over recent fetch history per model


def evaluate(cfg, city_key: str, climate_date_str: str) -> dict:
    city = cfg.cities[city_key]
    per_model: dict[str, float] = {}
    with get_session() as s:
        for model in city.models:
            rows = (
                s.query(ModelForecast.today_max)
                .filter(ModelForecast.city == city_key,
                        ModelForecast.model == model,
                        ModelForecast.climate_date == climate_date_str,
                        ModelForecast.today_max.isnot(None))
                .order_by(ModelForecast.ts_fetch.desc())
                .limit(MAX_FETCHES)
                .all()
            )
            vals = [r[0] for r in rows][::-1]  # chronological
            # collapse consecutive identical fetches into distinct runs
            runs = [v for i, v in enumerate(vals) if i == 0 or abs(v - vals[i - 1]) > 0.05]
            if len(runs) < 2:
                per_model[model] = 0.0
                continue
            deltas = [runs[i] - runs[i - 1] for i in range(1, len(runs))][-3:]
            same_sign = all(d > 0 for d in deltas) or all(d < 0 for d in deltas)
            per_model[model] = round(sum(deltas) / len(deltas), 2) if same_sign else 0.0
    trend_vals = [v for v in per_model.values() if v != 0.0]
    overall = round(sum(trend_vals) / len(trend_vals), 2) if trend_vals else 0.0
    return {"per_model": per_model, "overall": overall}
