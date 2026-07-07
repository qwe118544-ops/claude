"""Polymarket Gamma API ingestor — display only, never feeds the model.

Finds today's 'highest-temperature-in-{city}-on-{month}-{day}[-{year}]' event,
parses bucket titles and yes-prices, stores the top-N buckets by price.
"""
from __future__ import annotations

import datetime as dt
import json

from ..clock import climate_date, local_now
from ..db import MarketSnapshot, get_session
from ..pricing.buckets import parse_bucket
from .base import BaseIngestor, utcnow


class PolymarketIngestor(BaseIngestor):
    name = "polymarket"

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self.base = cfg.polymarket["gamma_base"]
        self.top_n = int(cfg.polymarket.get("top_n", 2))
        self._slug_cache: dict[tuple[str, str], str | None] = {}

    def _slug_candidates(self, city, date_local: dt.date) -> list[str]:
        month = date_local.strftime("%B").lower()
        day = date_local.day
        stem = f"highest-temperature-in-{city.market_slug}-on-{month}-{day}"
        return [f"{stem}-{date_local.year}", stem]

    async def poll_city(self, city_key: str) -> bool:
        city = self.cfg.cities[city_key]
        cdate = climate_date(city)
        date_local = local_now(city).date()

        cache_key = (city_key, cdate)
        slug = self._slug_cache.get(cache_key, "UNSET")
        event = None
        try:
            if slug not in ("UNSET", None):
                event = await self._fetch_event(slug)
            if event is None:
                for cand in self._slug_candidates(city, date_local):
                    event = await self._fetch_event(cand)
                    if event:
                        self._slug_cache[cache_key] = cand
                        slug = cand
                        break
                else:
                    self._slug_cache[cache_key] = None
                    self.log_latency(None, ok=False, note=f"{city_key}: no event for {cdate}")
                    return False
        except Exception as e:
            self.log_latency(None, ok=False, note=f"{city_key}: {type(e).__name__}: {e}")
            return False

        buckets = []
        for m in event.get("markets", []):
            title = m.get("groupItemTitle") or m.get("question") or ""
            rng = parse_bucket(title)
            yes = self._yes_price(m)
            if yes is None:
                continue
            lo, hi = rng if rng else (None, None)
            buckets.append({"title": title, "yes": yes, "lo": lo, "hi": hi})
        buckets.sort(key=lambda b: -b["yes"])
        top = buckets[: self.top_n]

        with get_session() as s:
            s.add(MarketSnapshot(city=city_key, climate_date=cdate, ts=utcnow(),
                                 event_slug=slug or "", buckets=top))
            s.commit()
        self.log_latency(utcnow(), ok=True, note=f"{city_key}: {len(buckets)} buckets")
        return True

    async def _fetch_event(self, slug: str) -> dict | None:
        r = await self.get(f"{self.base}/events", params={"slug": slug})
        arr = r.json()
        if isinstance(arr, list) and arr:
            return arr[0]
        return None

    @staticmethod
    def _yes_price(market: dict) -> float | None:
        """Gamma returns outcomes/outcomePrices as JSON-encoded strings."""
        try:
            outcomes = market.get("outcomes")
            prices = market.get("outcomePrices")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not outcomes or not prices:
                return None
            for o, p in zip(outcomes, prices):
                if str(o).lower() == "yes":
                    return float(p)
            return float(prices[0])
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    async def poll(self) -> list[str]:
        changed = []
        for key in self.cfg.cities:
            if await self.poll_city(key):
                changed.append(key)
        return changed
