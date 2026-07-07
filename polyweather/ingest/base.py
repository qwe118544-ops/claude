"""Ingestor base: one HTTP client, latency accounting, uniform error handling.

Every fetch records (source publish time vs our fetch time) into latency_log —
'faster than the order book' is a measurable claim, this table is the measure.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..config import USER_AGENT
from ..db import LatencyLog, get_session

log = logging.getLogger("polyweather.ingest")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class BaseIngestor:
    name = "base"

    def __init__(self, cfg, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    async def get(self, url: str, **kw) -> httpx.Response:
        r = await self.client.get(url, **kw)
        r.raise_for_status()
        return r

    def log_latency(self, ts_source: dt.datetime | None, ok: bool = True, note: str = ""):
        with get_session() as s:
            now = utcnow()
            lag = (now - ts_source).total_seconds() if ts_source else None
            s.add(LatencyLog(source=self.name, ts_fetch=now, ts_source=ts_source,
                             lag_s=lag, ok=ok, note=note[:250] or None))
            s.commit()

    async def poll(self) -> list[str]:
        """Fetch once. Returns list of city keys whose data changed
        (drives event-driven repricing)."""
        raise NotImplementedError


def wind_dir_in_sector(wdir: float | None, sector: list[float]) -> bool:
    """sector = [a, b] degrees FROM, clockwise a->b, possibly wrapping 360."""
    if wdir is None:
        return False
    a, b = sector
    w = wdir % 360
    if a <= b:
        return a <= w <= b
    return w >= a or w <= b


def dewpoint_from_rh(temp_c: float, rh: float) -> float:
    """Magnus formula."""
    import math
    a, b = 17.62, 243.12
    gamma = math.log(max(rh, 1.0) / 100.0) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)
