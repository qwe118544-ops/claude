#!/usr/bin/env python3
"""Lock down the exact rule Wunderground uses to compute an airport's daily
high temperature, by replaying 365 days of raw METARs through candidate rules
and comparing each day against Wunderground's own history values.

Why this matters: Polymarket temperature markets resolve against the daily
high shown on wunderground.com/history. That number is derived from METARs,
but the derivation (T-group tenths vs integer-degC body, rounding direction,
day boundary) is undocumented. This tool identifies the rule empirically:
whichever candidate reproduces 100% of the observed days is the rule.

Data sources (both free, no auth):
  * IEM ASOS archive (mesonet.agron.iastate.edu) - raw METAR text
  * api.weather.com v1 history - the same backend the Wunderground history
    page renders from (public site key)

Zero third-party dependencies: Python 3.9+ stdlib only.

Usage:
  python3 wu_rule_lock.py all --station LGA --wu-loc KLGA:9:US \
      --tz America/New_York --days 365 --outdir data/
  # or step by step:
  python3 wu_rule_lock.py fetch-iem --station LGA --start 2025-07-08 --end 2026-07-07 --out data/iem_LGA.csv
  python3 wu_rule_lock.py fetch-wu  --wu-loc KLGA:9:US --start 2025-07-08 --end 2026-07-07 --tz America/New_York --out data/wu_KLGA.csv
  python3 wu_rule_lock.py compare   --iem data/iem_LGA.csv --wu data/wu_KLGA.csv --tz America/New_York --outdir data/
  # if you scraped the ground truth yourself (CSV: date,high):
  python3 wu_rule_lock.py compare --iem data/iem_LGA.csv --truth-csv my_wu_highs.csv --tz America/New_York --outdir data/
  python3 wu_rule_lock.py selftest
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

# Public key embedded in wunderground.com's own pages; override with WU_API_KEY.
WU_DEFAULT_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# ---------------------------------------------------------------------------
# HTTP with retry/backoff
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 60) -> bytes:
    last_err = None
    for i, delay in enumerate((0, 2, 4, 8, 16)):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wu-rule-lock/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 - retry any transport error
            last_err = e
            sys.stderr.write(f"  fetch attempt {i + 1} failed: {e}\n")
    raise RuntimeError(f"GET failed after retries: {url}\n  last error: {last_err}")


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_iem(station: str, start: date, end: date, out_path: str) -> None:
    """Download raw METARs (routine + specials) from IEM, in UTC.

    We pad one day on each side so local-day bucketing never misses an ob.
    Output CSV columns: valid_utc, metar
    """
    pad_start = start - timedelta(days=1)
    pad_end = end + timedelta(days=2)  # asos.py end is exclusive-ish; pad hard
    params = [
        ("station", station),
        ("data", "metar"),
        ("year1", pad_start.year), ("month1", pad_start.month), ("day1", pad_start.day),
        ("year2", pad_end.year), ("month2", pad_end.month), ("day2", pad_end.day),
        ("tz", "UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "M"),
        ("trace", "T"),
        ("direct", "no"),
        ("report_type", "3"),  # routine METAR
        ("report_type", "4"),  # SPECI
    ]
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(params)
    print(f"IEM: fetching {station} {pad_start} .. {pad_end} (UTC, routine+SPECI)")
    raw = http_get(url, timeout=300).decode("utf-8", errors="replace")

    rows = []
    seen = set()
    reader = csv.DictReader(raw.splitlines())
    for r in reader:
        valid, metar = r.get("valid", "").strip(), r.get("metar", "").strip()
        if not valid or not metar:
            continue
        key = (valid, metar)
        if key in seen:
            continue
        seen.add(key)
        rows.append((valid, metar))
    rows.sort()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["valid_utc", "metar"])
        w.writerows(rows)
    print(f"IEM: wrote {len(rows)} reports -> {out_path}")


def fetch_wu(wu_loc: str, start: date, end: date, tz_name: str, out_path: str) -> None:
    """Download the observation table behind the WU daily-history page.

    api.weather.com v1 historical accepts <=31-day windows. Each record's
    integer `temp` (units=e -> deg F) is exactly what the WU obs table shows;
    the page's "High Temp" summary is the max of those rows over the local day.
    Output CSV columns: epoch_gmt, local_time, temp_f
    """
    key = os.environ.get("WU_API_KEY", WU_DEFAULT_KEY)
    tz = ZoneInfo(tz_name)
    rows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=29), end)
        url = (
            f"https://api.weather.com/v1/location/{urllib.parse.quote(wu_loc)}"
            f"/observations/historical.json?apiKey={key}&units=e"
            f"&startDate={cur:%Y%m%d}&endDate={chunk_end:%Y%m%d}"
        )
        print(f"WU: fetching {cur} .. {chunk_end}")
        payload = json.loads(http_get(url, timeout=120))
        for ob in payload.get("observations") or []:
            epoch, temp = ob.get("valid_time_gmt"), ob.get("temp")
            if epoch is None or temp is None:
                continue
            local = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz)
            rows.append((epoch, local.isoformat(), int(temp)))
        cur = chunk_end + timedelta(days=1)
        time.sleep(1)  # be polite
    rows.sort()
    dedup = []
    seen = set()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        dedup.append(r)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["epoch_gmt", "local_time", "temp_f"])
        w.writerows(dedup)
    print(f"WU: wrote {len(dedup)} obs -> {out_path}")


# ---------------------------------------------------------------------------
# METAR parsing (exact, rational arithmetic)
# ---------------------------------------------------------------------------

BODY_TEMP_RE = re.compile(r"(?:^|\s)(M?\d{2})/(?:M?\d{2})?(?=\s|$)")
TGROUP_RE = re.compile(r"(?:^|\s)T([01]\d{3})[01]\d{3}(?=\s|$)")


def parse_metar_temps(metar: str):
    """Return (body_c: int|None, tgroup_c: Fraction|None) from a raw METAR."""
    body, _, remarks = metar.partition(" RMK ")
    body_c = None
    m = BODY_TEMP_RE.search(body)
    if m:
        tok = m.group(1)
        body_c = -int(tok[1:]) if tok.startswith("M") else int(tok)
    tgroup_c = None
    m = TGROUP_RE.search(remarks)
    if m:
        tok = m.group(1)
        sign = -1 if tok[0] == "1" else 1
        tgroup_c = Fraction(sign * int(tok[1:]), 10)
    return body_c, tgroup_c


def c_to_f(c) -> Fraction:
    return Fraction(c) * Fraction(9, 5) + 32


def round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def round_half_even(x: Fraction) -> int:
    return round(x)


def round_trunc(x: Fraction) -> int:
    return int(x)  # toward zero


def round_floor(x: Fraction) -> int:
    return math.floor(x)


ROUNDINGS = {
    "half_up": round_half_up,
    "half_even": round_half_even,
    "trunc": round_trunc,
    "floor": round_floor,
}


def ob_temp_c(body_c, tgroup_c, source):
    """Pick the Celsius value an ob contributes under a given source rule."""
    if source == "tgroup_pref":       # T-group tenths, fall back to body
        return tgroup_c if tgroup_c is not None else (Fraction(body_c) if body_c is not None else None)
    if source == "body_int":          # integer degC body only
        return Fraction(body_c) if body_c is not None else None
    if source == "tgroup_only":       # skip obs lacking a T-group
        return tgroup_c
    if source == "tgroup_to_intC":    # T-group rounded to whole degC first
        if tgroup_c is not None:
            return Fraction(round_half_up(tgroup_c))
        return Fraction(body_c) if body_c is not None else None
    raise ValueError(source)


SOURCES = ["tgroup_pref", "body_int", "tgroup_only", "tgroup_to_intC"]


# ---------------------------------------------------------------------------
# Daily-high computation under candidate rules
# ---------------------------------------------------------------------------

def load_iem(path: str):
    obs = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            dt = datetime.strptime(r["valid_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            body_c, tgroup_c = parse_metar_temps(r["metar"])
            if body_c is None and tgroup_c is None:
                continue
            obs.append((dt, body_c, tgroup_c, r["metar"]))
    return obs


def bucket_local_day(dt_utc: datetime, tz, boundary: str):
    if boundary == "local":  # wall clock, DST-aware (what the WU page shows)
        return dt_utc.astimezone(tz).date()
    if boundary == "lst":    # fixed standard-time offset (NWS climate day)
        std_offset = tz.utcoffset(datetime(2020, 1, 15))  # mid-January = standard time
        return (dt_utc + std_offset).replace(tzinfo=None).date()
    raise ValueError(boundary)


def daily_highs(obs, tz, source: str, rounding: str, boundary: str, order: str):
    """order: 'round_each' (round every ob, then max) or 'max_first'
    (max exact degF, then round). Provably identical for monotone roundings;
    both kept so the equivalence is *demonstrated*, not assumed."""
    rnd = ROUNDINGS[rounding]
    per_day = {}
    for dt_utc, body_c, tgroup_c, _ in obs:
        c = ob_temp_c(body_c, tgroup_c, source)
        if c is None:
            continue
        d = bucket_local_day(dt_utc, tz, boundary)
        f_exact = c_to_f(c)
        if order == "round_each":
            v = rnd(f_exact)
            per_day[d] = max(per_day.get(d, -10**9), v)
        else:
            cur = per_day.get(d)
            per_day[d] = f_exact if cur is None or f_exact > cur else cur
    if order == "max_first":
        per_day = {d: rnd(v) for d, v in per_day.items()}
    return per_day


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def truth_from_wu_csv(path: str, tz):
    per_day = {}
    counts = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            local = datetime.fromisoformat(r["local_time"])
            d = local.date()
            t = int(r["temp_f"])
            per_day[d] = max(per_day.get(d, -10**9), t)
            counts[d] = counts.get(d, 0) + 1
    return per_day, counts


def truth_from_plain_csv(path: str):
    per_day = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            per_day[date.fromisoformat(r["date"].strip())] = int(float(r["high"]))
    return per_day, {d: -1 for d in per_day}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare(iem_path, wu_path, truth_csv, tz_name, start, end, outdir, min_obs=18):
    tz = ZoneInfo(tz_name)
    obs = load_iem(iem_path)
    print(f"Loaded {len(obs)} METARs with a temperature from {iem_path}")

    if truth_csv:
        truth, counts = truth_from_plain_csv(truth_csv)
        print(f"Ground truth: {len(truth)} days from {truth_csv} (external scrape)")
    else:
        truth, counts = truth_from_wu_csv(wu_path, tz)
        print(f"Ground truth: {len(truth)} days from WU obs table max ({wu_path})")

    days = sorted(d for d in truth if start <= d <= end)
    # Drop days where WU's own record is too sparse to trust as truth.
    full_days = [d for d in days if counts.get(d, 0) >= min_obs or counts.get(d, 0) == -1]
    skipped = [d for d in days if d not in set(full_days)]
    if skipped:
        print(f"Skipping {len(skipped)} sparse WU days (<{min_obs} obs): {', '.join(map(str, skipped[:10]))}"
              + (" ..." if len(skipped) > 10 else ""))

    results = []
    rule_days = {}
    for source in SOURCES:
        for rounding in ROUNDINGS:
            for boundary in ("local", "lst"):
                a = daily_highs(obs, tz, source, rounding, boundary, "round_each")
                b = daily_highs(obs, tz, source, rounding, boundary, "max_first")
                assert a == b, "max/round commutation violated?!"  # monotone => identical
                name = f"{source}|{rounding}|{boundary}"
                rule_days[name] = a
                mismatches = [d for d in full_days if a.get(d) != truth[d]]
                results.append((name, len(full_days) - len(mismatches), len(full_days), mismatches))

    results.sort(key=lambda r: -r[1])
    print(f"\n=== Rule ranking over {len(full_days)} days "
          f"(source | degF rounding | day boundary) ===")
    print(f"{'rule':45s} {'match':>6s} {'total':>6s} {'rate':>8s}")
    for name, ok, total, mism in results:
        print(f"{name:45s} {ok:6d} {total:6d} {ok / total * 100:7.2f}%")

    winners = [r for r in results if r[1] == r[2]]
    os.makedirs(outdir, exist_ok=True)

    # Per-day dump for the best rule + mismatch diagnostics for top 3 rules.
    best = results[0]
    with open(os.path.join(outdir, "daily_comparison.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "wu_high", best[0], "match"])
        for d in full_days:
            v = rule_days[best[0]].get(d)
            w.writerow([d, truth[d], v, int(v == truth[d])])

    with open(os.path.join(outdir, "mismatches.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule", "date", "wu_high", "rule_high", "hottest_metars"])
        for name, ok, total, mism in results[:3]:
            hs = rule_days[name]
            for d in mism:
                src, rnd, bnd = name.split("|")
                hot = hottest_obs(obs, tz, d, src, bnd)
                w.writerow([name, d, truth[d], hs.get(d), hot])

    print(f"\nWrote {outdir}/daily_comparison.csv and {outdir}/mismatches.csv")
    if winners:
        print("\n*** 100% rules ***")
        for name, ok, total, _ in winners:
            print(f"  {name}  ({ok}/{total})")
    else:
        name, ok, total, mism = best
        print(f"\nNo rule hit 100%. Best: {name} at {ok}/{total}.")
        print(f"First mismatches: {', '.join(map(str, mism[:8]))}")
        print("Inspect mismatches.csv — usual suspects: SPECIs WU ingested that IEM "
              "filed differently, WU backfilling from synoptic 6-h max groups, or a "
              "day-boundary edge (check the 'lst' variants' dates).")
    return results


def hottest_obs(obs, tz, d, source, boundary, top=3):
    scored = []
    for dt_utc, body_c, tgroup_c, metar in obs:
        if bucket_local_day(dt_utc, tz, boundary) != d:
            continue
        c = ob_temp_c(body_c, tgroup_c, source)
        if c is None:
            continue
        scored.append((float(c_to_f(c)), dt_utc.astimezone(tz).strftime("%H:%M"), metar[:60]))
    scored.sort(reverse=True)
    return " ; ".join(f"{t:.2f}F@{hh} {m}" for t, hh, m in scored[:top])


# ---------------------------------------------------------------------------
# Self-test (offline)
# ---------------------------------------------------------------------------

def selftest():
    # T-group parsing, positive and negative
    b, t = parse_metar_temps(
        "KLGA 011251Z 31008KT 10SM FEW250 24/12 A3012 RMK AO2 SLP198 T02440122")
    assert b == 24 and t == Fraction(244, 10), (b, t)
    b, t = parse_metar_temps(
        "KLGA 151251Z 31008KT 10SM M03/M08 A3012 RMK AO2 T10281083")
    assert b == -3 and t == Fraction(-28, 10), (b, t)
    b, t = parse_metar_temps("KLGA 151251Z 31008KT 10SM A3012 RMK AO2")
    assert b is None and t is None
    # body temp with missing dewpoint
    b, t = parse_metar_temps("KLGA 151251Z 00000KT 10SM 07/ A3012 RMK AO2")
    assert b == 7 and t is None, (b, t)

    # Exact half-degF case: 22.5 C -> exactly 72.5 F. Floats get this wrong;
    # Fractions must not.
    f = c_to_f(Fraction(225, 10))
    assert f == Fraction(145, 2)
    assert round_half_up(f) == 73 and round_half_even(f) == 72
    assert round_trunc(f) == 72 and round_floor(f) == 72
    # Negative half: -0.28C -> 31.496F ; and exact -13.5F from -25.2777..? keep simple:
    assert round_half_up(Fraction(-27, 2)) == -13   # -13.5 -> -13 (toward +inf)
    assert round_half_even(Fraction(-27, 2)) == -14
    assert round_trunc(Fraction(-27, 2)) == -13
    assert round_floor(Fraction(-27, 2)) == -14
    # Integer-degC bodies can never land on .5 F: (9c+160)/5 has no .5 residue.
    assert all((c_to_f(c) - math.floor(c_to_f(c))) != Fraction(1, 2) for c in range(-40, 50))

    # round-each vs max-first equivalence on a synthetic day
    tz = ZoneInfo("America/New_York")
    obs = []
    base = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)
    import random
    rng = random.Random(42)
    for i in range(200):
        tenths = rng.randint(-300, 400)
        obs.append((base + timedelta(minutes=7 * i),
                    round_half_up(Fraction(tenths, 10)), Fraction(tenths, 10), "X"))
    for source in SOURCES:
        for rounding in ROUNDINGS:
            for boundary in ("local", "lst"):
                a = daily_highs(obs, tz, source, rounding, boundary, "round_each")
                b = daily_highs(obs, tz, source, rounding, boundary, "max_first")
                assert a == b, (source, rounding, boundary)

    # LST vs local bucketing: 2026-07-02 03:30 UTC = 23:30 EDT Jul 1 (local),
    # but 22:30 EST -> still Jul 1 under LST. 2026-07-02 04:30 UTC = 00:30 EDT
    # Jul 2 local, but 23:30 EST Jul 1 under LST -> the discriminating hour.
    dt = datetime(2026, 7, 2, 4, 30, tzinfo=timezone.utc)
    assert bucket_local_day(dt, tz, "local") == date(2026, 7, 2)
    assert bucket_local_day(dt, tz, "lst") == date(2026, 7, 1)

    print("selftest: all assertions passed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_dates(sp):
        sp.add_argument("--start", type=date.fromisoformat)
        sp.add_argument("--end", type=date.fromisoformat)
        sp.add_argument("--days", type=int, default=365)

    sp = sub.add_parser("fetch-iem")
    sp.add_argument("--station", required=True, help="IEM id, e.g. LGA (no K)")
    add_dates(sp)
    sp.add_argument("--out", required=True)

    sp = sub.add_parser("fetch-wu")
    sp.add_argument("--wu-loc", required=True, help="e.g. KLGA:9:US")
    sp.add_argument("--tz", default="America/New_York")
    add_dates(sp)
    sp.add_argument("--out", required=True)

    sp = sub.add_parser("compare")
    sp.add_argument("--iem", required=True)
    sp.add_argument("--wu")
    sp.add_argument("--truth-csv", help="external ground truth CSV: date,high")
    sp.add_argument("--tz", default="America/New_York")
    add_dates(sp)
    sp.add_argument("--outdir", default="out")
    sp.add_argument("--min-obs", type=int, default=18)

    sp = sub.add_parser("all")
    sp.add_argument("--station", required=True)
    sp.add_argument("--wu-loc", required=True)
    sp.add_argument("--tz", default="America/New_York")
    add_dates(sp)
    sp.add_argument("--outdir", default="data")

    sub.add_parser("selftest")

    a = p.parse_args()
    if a.cmd == "selftest":
        return selftest()

    end = a.end or (date.today() - timedelta(days=1))
    start = a.start or (end - timedelta(days=a.days - 1))

    if a.cmd == "fetch-iem":
        fetch_iem(a.station, start, end, a.out)
    elif a.cmd == "fetch-wu":
        fetch_wu(a.wu_loc, start, end, a.tz, a.out)
    elif a.cmd == "compare":
        if not a.wu and not a.truth_csv:
            p.error("compare needs --wu or --truth-csv")
        compare(a.iem, a.wu, a.truth_csv, a.tz, start, end, a.outdir, a.min_obs)
    elif a.cmd == "all":
        iem_csv = os.path.join(a.outdir, f"iem_{a.station}.csv")
        wu_csv = os.path.join(a.outdir, f"wu_{a.wu_loc.split(':')[0]}.csv")
        fetch_iem(a.station, start, end, iem_csv)
        fetch_wu(a.wu_loc, start, end, a.tz, wu_csv)
        compare(iem_csv, wu_csv, None, a.tz, start, end, a.outdir, 18)


if __name__ == "__main__":
    main()
