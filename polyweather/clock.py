"""Local-time day state machine.

The climate day is the local calendar day (what the Wunderground history table
— i.e. the settlement source — shows). Phases drive polling cadence and
engine behaviour. The midnight-max trap is handled explicitly: warm-advection
nights can put the daily max at 00:01 local, so NIGHT still observes and the
running max starts accumulating from local midnight.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .config import CityConfig

PHASES = ("NIGHT", "MORNING", "PEAK", "LOCKIN", "SETTLED")


def local_now(city: CityConfig, now_utc: dt.datetime | None = None) -> dt.datetime:
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    return now_utc.astimezone(ZoneInfo(city.tz))


def climate_date(city: CityConfig, now_utc: dt.datetime | None = None) -> str:
    return local_now(city, now_utc).date().isoformat()


def climate_day_bounds_utc(city: CityConfig, date_local: str) -> tuple[dt.datetime, dt.datetime]:
    """UTC [start, end) of the local calendar day."""
    tz = ZoneInfo(city.tz)
    d = dt.date.fromisoformat(date_local)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=tz)
    end = start + dt.timedelta(days=1)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def _parse_hm(s: str) -> dt.time:
    if s == "24:00":
        return dt.time(23, 59, 59)
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def day_phase(city: CityConfig, summer_months: list[int],
              now_utc: dt.datetime | None = None) -> str:
    loc = local_now(city, now_utc)
    peak_start, peak_end = city.peak_window(loc.month, summer_months)
    t = loc.time()
    ps, pe = _parse_hm(peak_start), _parse_hm(peak_end)
    morning_start = dt.time(6, 0)
    lockin_end = dt.time(19, 0)
    if t < morning_start:
        return "NIGHT"
    if t < ps:
        return "MORNING"
    if t < pe:
        return "PEAK"
    if t < lockin_end:
        return "LOCKIN"
    return "SETTLED"


def hours_left_in_warming_window(city: CityConfig, summer_months: list[int],
                                 now_utc: dt.datetime | None = None) -> float:
    """Hours from now until the end of the peak window (0 if past)."""
    loc = local_now(city, now_utc)
    _, peak_end = city.peak_window(loc.month, summer_months)
    pe = _parse_hm(peak_end)
    end = loc.replace(hour=pe.hour, minute=pe.minute, second=0, microsecond=0)
    delta = (end - loc).total_seconds() / 3600.0
    return max(0.0, delta)


def peak_center_minutes(city: CityConfig, summer_months: list[int], month: int) -> float:
    """Minutes-after-midnight of the climatological peak (center of window)."""
    ps, pe = city.peak_window(month, summer_months)
    a, b = _parse_hm(ps), _parse_hm(pe)
    return ((a.hour * 60 + a.minute) + (b.hour * 60 + b.minute)) / 2.0
