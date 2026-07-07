import datetime as dt

from polyweather import clock
from polyweather.config import get_config

CFG = get_config()


def test_climate_day_bounds_tokyo():
    tokyo = CFG.cities["tokyo"]
    start, end = clock.climate_day_bounds_utc(tokyo, "2026-07-07")
    assert start.isoformat() == "2026-07-06T15:00:00+00:00"  # JST midnight
    assert (end - start) == dt.timedelta(days=1)


def test_phases_tokyo_summer():
    tokyo = CFG.cities["tokyo"]
    sm = CFG.summer_months

    def phase_at(hour_jst, minute=0):
        utc = dt.datetime(2026, 7, 7, hour_jst, minute,
                          tzinfo=dt.timezone(dt.timedelta(hours=9)))
        return clock.day_phase(tokyo, sm, utc.astimezone(dt.timezone.utc))

    assert phase_at(3) == "NIGHT"
    assert phase_at(8) == "MORNING"
    assert phase_at(12, 30) == "PEAK"
    assert phase_at(16) == "LOCKIN"
    assert phase_at(21) == "SETTLED"


def test_hours_left():
    tokyo = CFG.cities["tokyo"]
    sm = CFG.summer_months
    # 12:30 JST on a July day; peak window ends 14:30 -> 2h left
    utc = dt.datetime(2026, 7, 7, 12, 30,
                      tzinfo=dt.timezone(dt.timedelta(hours=9))).astimezone(dt.timezone.utc)
    h = clock.hours_left_in_warming_window(tokyo, sm, utc)
    assert abs(h - 2.0) < 0.01
    # evening -> 0
    utc = dt.datetime(2026, 7, 7, 20, 0,
                      tzinfo=dt.timezone(dt.timedelta(hours=9))).astimezone(dt.timezone.utc)
    assert clock.hours_left_in_warming_window(tokyo, sm, utc) == 0.0


def test_winter_window_differs():
    london = CFG.cities["london"]
    assert london.peak_window(7, CFG.summer_months) == ("13:00", "16:30")
    assert london.peak_window(1, CFG.summer_months) == ("11:30", "14:30")
