"""Event-driven scheduling: per-source polling loops whose cadence follows
each city's day phase; any new data triggers a reprice of the affected city,
which is pushed to SSE subscribers via the bus.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .bus import BUS
from .clock import day_phase
from .config import get_config
from .db import init_db
from .evaluate import settle_yesterday_all
from .ingest.amedas import AmedasIngestor
from .ingest.knmi import KnmiIngestor
from .ingest.metar import MetarIngestor
from .ingest.openmeteo import OpenMeteoIngestor
from .ingest.polymarket import PolymarketIngestor
from .pricing.engine import Engine

log = logging.getLogger("polyweather.scheduler")


class Runtime:
    def __init__(self):
        self.cfg = get_config()
        init_db()
        self.engine = Engine(self.cfg)
        self.metar = MetarIngestor(self.cfg)
        self.amedas = AmedasIngestor(self.cfg)
        self.knmi = KnmiIngestor(self.cfg)
        self.openmeteo = OpenMeteoIngestor(self.cfg)
        self.polymarket = PolymarketIngestor(self.cfg)
        self._tasks: list[asyncio.Task] = []
        self._repricing: set[str] = set()

    # -- cadence: the FASTEST phase across cities decides shared sources --
    def _cadence(self, source: str) -> int:
        vals = []
        for key, city in self.cfg.cities.items():
            phase = day_phase(city, self.cfg.summer_months)
            vals.append(self.cfg.cadence[phase][source])
        return min(vals)

    def _cadence_city(self, source: str, city_key: str) -> int:
        city = self.cfg.cities[city_key]
        phase = day_phase(city, self.cfg.summer_months)
        return self.cfg.cadence[phase][source]

    async def reprice(self, city_key: str, reason: str):
        if city_key in self._repricing:
            return
        self._repricing.add(city_key)
        try:
            out = await asyncio.to_thread(self.engine.recompute, city_key)
            if out:
                await BUS.publish({"type": "pricing", "city": city_key,
                                   "reason": reason, "data": out})
        except Exception:
            log.exception("reprice %s failed", city_key)
        finally:
            self._repricing.discard(city_key)

    async def _loop_obs(self, ingestor, source_key: str):
        while True:
            try:
                changed = await ingestor.poll()
                for city_key in changed:
                    await self.reprice(city_key, reason=ingestor.name)
                    await BUS.publish({"type": "obs", "city": city_key,
                                       "source": ingestor.name})
            except Exception:
                log.exception("%s poll failed", ingestor.name)
            await asyncio.sleep(self._cadence(source_key))

    async def _loop_models(self):
        while True:
            for city_key in self.cfg.cities:
                try:
                    if await self.openmeteo.poll_city(city_key):
                        await self.reprice(city_key, reason="models")
                except Exception:
                    log.exception("openmeteo %s failed", city_key)
            await asyncio.sleep(self._cadence("models"))

    async def _loop_market(self):
        while True:
            for city_key in self.cfg.cities:
                try:
                    if await self.polymarket.poll_city(city_key):
                        await BUS.publish({"type": "market", "city": city_key})
                except Exception:
                    log.exception("polymarket %s failed", city_key)
            await asyncio.sleep(self._cadence("market"))

    async def _loop_settle(self):
        while True:
            try:
                results = await asyncio.to_thread(settle_yesterday_all, self.cfg)
                for r in results:
                    await BUS.publish({"type": "settled", **r})
            except Exception:
                log.exception("settle failed")
            await asyncio.sleep(3600)

    def start(self):
        self._tasks = [
            asyncio.create_task(self._loop_obs(self.metar, "metar")),
            asyncio.create_task(self._loop_obs(self.amedas, "fast")),
            asyncio.create_task(self._loop_models()),
            asyncio.create_task(self._loop_market()),
            asyncio.create_task(self._loop_settle()),
        ]
        if self.knmi.enabled:
            self._tasks.append(asyncio.create_task(self._loop_obs(self.knmi, "fast")))
        else:
            log.warning("KNMI ingestor disabled (no KNMI_API_KEY) — "
                        "Amsterdam runs on METAR only")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
