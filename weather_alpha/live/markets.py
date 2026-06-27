"""The live market data model and a best-effort question parser.

A Polymarket weather market is a binary question like:
    "Will the high temperature in New York be 90°F or above on July 15?"

To trade it automatically we must extract a structured contract:
    city, threshold, direction (>= / <=), settlement date.

Question wording is not standardised, so `parse_weather_question` is
best-effort and returns a confidence flag. Anything low-confidence should be
skipped rather than traded on a guess — getting the settlement definition
wrong is the #1 way to lose with a "correct" model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional


@dataclass
class WeatherMarket:
    market_id: str
    question: str
    yes_token_id: str

    # Parsed contract (filled by parse_weather_question / a geocoder).
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone_name: str = "auto"
    threshold_f: Optional[float] = None
    direction: str = ">="          # ">=" means "temp at or above threshold"
    settle_date: Optional[date] = None
    parse_confident: bool = False

    # Live market state (filled by the client).
    yes_price: Optional[float] = None     # implied P(YES); mid or last
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    top_ask_size: Optional[float] = None  # tradeable size at best ask (USDC)
    top_bid_size: Optional[float] = None
    settle_dt_utc: Optional[datetime] = None

    notes: list = field(default_factory=list)

    def hours_to_settle(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.settle_dt_utc is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.settle_dt_utc - now).total_seconds() / 3600.0

    def is_tradeable_contract(self) -> bool:
        return (
            self.parse_confident
            and self.latitude is not None
            and self.longitude is not None
            and self.threshold_f is not None
            and self.settle_date is not None
        )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}
# accept 3-letter abbreviations too
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})

_TEMP_RE = re.compile(r"(-?\d{1,3})\s*°?\s*(?:f|fahrenheit|degrees)?", re.I)
_ABOVE_RE = re.compile(r"\b(above|over|at least|or (?:above|higher|more)|>=|≥|exceed|hotter)\b", re.I)
_BELOW_RE = re.compile(r"\b(below|under|at most|or (?:below|lower|less)|<=|≤|colder)\b", re.I)
_DATE_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS.keys(), key=len, reverse=True)) + r")\.?\s+(\d{1,2})\b",
    re.I,
)


def parse_weather_question(
    question: str,
    default_year: Optional[int] = None,
) -> dict:
    """Best-effort extraction of (threshold_f, direction, settle_date).

    Returns a dict with keys: threshold_f, direction, settle_date, confident,
    notes. City/lat/lon are NOT inferred here (use a geocoder / lookup table);
    the scanner expects a city->coords mapping to be supplied.
    """
    notes = []
    q = question.strip()

    # Threshold: prefer a number adjacent to a temperature cue.
    threshold = None
    temp_cue = re.search(r"(-?\d{1,3})\s*°?\s*(?:f\b|fahrenheit|degree)", q, re.I)
    if temp_cue:
        threshold = float(temp_cue.group(1))
    else:
        m = _TEMP_RE.search(q)
        if m:
            threshold = float(m.group(1))
            notes.append("threshold taken from a bare number; verify units")

    # Direction.
    if _ABOVE_RE.search(q):
        direction = ">="
    elif _BELOW_RE.search(q):
        direction = "<="
    else:
        direction = ">="
        notes.append("no explicit direction cue; assumed '>='")

    # Date.
    settle_date = None
    dm = _DATE_RE.search(q)
    if dm:
        month = _MONTHS[dm.group(1).lower()]
        day = int(dm.group(2))
        year = default_year or datetime.now(timezone.utc).year
        try:
            settle_date = date(year, month, day)
        except ValueError:
            notes.append(f"invalid date {dm.group(0)}")

    confident = (
        threshold is not None
        and settle_date is not None
        and not any("bare number" in n for n in notes)
    )
    if threshold is None:
        notes.append("could not parse a temperature threshold")
    if settle_date is None:
        notes.append("could not parse a settlement date")

    return {
        "threshold_f": threshold,
        "direction": direction,
        "settle_date": settle_date,
        "confident": confident,
        "notes": notes,
    }
