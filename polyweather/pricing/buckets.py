"""Mapping between integer-°C settlement PMFs and Polymarket bucket titles.

Bucket titles observed on Polymarket temperature markets:
  "24°C or below" / "25°C" / "26°C" / ... / "30°C or higher"
(and °F variants for US cities, not used here).
"""
from __future__ import annotations

import re

_RE_EXACT = re.compile(r"^(-?\d+)\s*°?\s*C$", re.I)
_RE_BELOW = re.compile(r"^(-?\d+)\s*°?\s*C\s*or\s*(below|lower|less)", re.I)
_RE_ABOVE = re.compile(r"^(-?\d+)\s*°?\s*C\s*or\s*(above|higher|more)", re.I)
_RE_RANGE = re.compile(r"^(-?\d+)\s*[-–]\s*(-?\d+)\s*°?\s*C$", re.I)


def parse_bucket(title: str) -> tuple[float, float] | None:
    """Return (lo, hi) inclusive integer bounds; +-inf for open ends."""
    t = title.strip()
    m = _RE_BELOW.match(t)
    if m:
        return float("-inf"), float(m.group(1))
    m = _RE_ABOVE.match(t)
    if m:
        return float(m.group(1)), float("inf")
    m = _RE_RANGE.match(t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _RE_EXACT.match(t)
    if m:
        v = float(m.group(1))
        return v, v
    return None


def bucket_prob(int_pmf: dict[int, float], lo: float, hi: float) -> float:
    return sum(p for j, p in int_pmf.items() if lo <= j <= hi)
