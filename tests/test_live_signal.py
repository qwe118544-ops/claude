"""Offline correctness checks for the LIVE signal engine (no network).

Run:  PYTHONPATH=. python tests/test_live_signal.py

Covers the parts that decide whether real money would move: forecast->prob,
edge + side selection, the four-condition filter, taker-price selection, and
the scanner end-to-end with a fake client (incl. JSONL track-record logging).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np

from weather_alpha.live import (
    Scanner,
    SignalParams,
    WeatherMarket,
    evaluate_signal,
    parse_weather_question,
    probability_from_members,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def _market(**kw) -> WeatherMarket:
    base = dict(
        market_id="m1",
        question="Will the high temperature in New York be 90°F or above on July 15?",
        yes_token_id="tok-yes",
        city="new york", latitude=40.78, longitude=-73.97,
        threshold_f=90.0, direction=">=", settle_date=None, parse_confident=True,
        yes_price=0.50, best_bid=0.48, best_ask=0.52,
        top_ask_size=500.0, top_bid_size=500.0,
        settle_dt_utc=NOW + timedelta(hours=24),
    )
    base.update(kw)
    from datetime import date
    if base["settle_date"] is None:
        base["settle_date"] = date(2026, 7, 15)
    return WeatherMarket(**base)


def test_probability_from_members():
    members = np.array([88, 89, 90, 91, 92, 93, 94, 95, 96, 97], dtype=float)
    # 8 of 10 are >= 90; Laplace smoothing(1) -> (8+1)/(10+2) = 0.75
    p = probability_from_members(members, 90, ">=", smoothing=1.0)
    assert abs(p - 0.75) < 1e-9, p
    # bias correction shifts members down 3 -> only 5 of 10 >= 90 -> (5+1)/12
    p2 = probability_from_members(members, 90, ">=", bias_correction=-3.0, smoothing=1.0)
    assert abs(p2 - 6 / 12) < 1e-9, p2
    # direction "<=" : 3 members (88,89,90) <= 90 -> (3+1)/12
    p3 = probability_from_members(members, 90, "<=", smoothing=1.0)
    assert abs(p3 - 4 / 12) < 1e-9, p3
    print("  ok: member counting, bias, direction, smoothing")


def test_buy_yes_when_market_underprices():
    m = _market(yes_price=0.50, best_ask=0.52)
    s = evaluate_signal(m, my_prob=0.70, params=SignalParams(min_edge=0.08), now=NOW)
    assert s.side == "BUY_YES" and s.tradeable, s.reasons
    assert s.edge > 0
    print(f"  ok: BUY_YES, {s.reasons[-1]}")


def test_buy_no_when_market_overprices():
    m = _market(yes_price=0.50, best_bid=0.48)
    s = evaluate_signal(m, my_prob=0.30, params=SignalParams(min_edge=0.08), now=NOW)
    assert s.side == "BUY_NO" and s.tradeable, s.reasons
    assert s.edge < 0
    print(f"  ok: BUY_NO, {s.reasons[-1]}")


def test_no_trade_inside_edge_band():
    m = _market(yes_price=0.50)
    s = evaluate_signal(m, my_prob=0.54, params=SignalParams(min_edge=0.08), now=NOW)
    assert not s.tradeable and s.side is None
    print("  ok: no trade when edge below threshold")


def test_thin_liquidity_blocks_trade():
    m = _market(yes_price=0.50, best_ask=0.52, top_ask_size=10.0)
    s = evaluate_signal(m, my_prob=0.70,
                        params=SignalParams(min_edge=0.08, min_liquidity_usdc=50.0), now=NOW)
    assert not s.tradeable and any("thin book" in r for r in s.reasons), s.reasons
    print("  ok: thin liquidity blocks an otherwise-good edge")


def test_too_far_to_settle_blocks_trade():
    m = _market(yes_price=0.50, settle_dt_utc=NOW + timedelta(hours=200))
    s = evaluate_signal(m, my_prob=0.70,
                        params=SignalParams(min_edge=0.08, max_hours_to_settle=96), now=NOW)
    assert not s.tradeable and any("too far out" in r for r in s.reasons), s.reasons
    print("  ok: far-out settlement blocks trade (forecast not trustworthy yet)")


def test_unconfident_contract_blocks_trade():
    m = _market(yes_price=0.50, best_ask=0.52, parse_confident=False)
    s = evaluate_signal(m, my_prob=0.70, params=SignalParams(min_edge=0.08), now=NOW)
    assert not s.tradeable and any("not parsed confidently" in r for r in s.reasons)
    print("  ok: unconfident contract is never traded")


def test_question_parser():
    p = parse_weather_question(
        "Will the high temperature in Chicago be 95°F or above on August 3?")
    assert p["threshold_f"] == 95.0 and p["direction"] == ">=" and p["confident"]
    assert (p["settle_date"].month, p["settle_date"].day) == (8, 3)
    below = parse_weather_question("Will NYC low be below 32 degrees on January 9?")
    assert below["direction"] == "<="
    print("  ok: question parser (threshold, direction, date)")


class _FakeClient:
    def __init__(self, markets):
        self._markets = markets

    def list_weather_markets(self, limit=200):
        return list(self._markets)

    def fill_book(self, market):
        return market  # books already set on the fakes


class _FakeForecast:
    def __init__(self, prob):
        self._p = prob

    def probability(self, target, threshold, direction=">=", bias_correction=0.0):
        return self._p


def test_scanner_end_to_end_with_logging():
    good = _market(market_id="good", yes_price=0.50, best_ask=0.52)
    weak = _market(market_id="weak", yes_price=0.50)  # forecast will match -> no edge
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "signals.jsonl")
        # forecast factory returns a strong YES view for all markets (0.72)
        scanner = Scanner(_FakeClient([good, weak]),
                          forecast_factory=lambda m: _FakeForecast(0.72),
                          params=SignalParams(min_edge=0.08), log_path=log)
        report = scanner.scan_once(now=NOW)
        assert report.n_markets == 2
        assert report.n_tradeable == 2  # both underpriced at 0.50 vs our 0.72
        lines = [json.loads(x) for x in open(log)]
        assert len(lines) == 2 and all("ts" in r and "edge" in r for r in lines)
    print(f"  ok: scanner scanned {report.n_markets}, "
          f"{report.n_tradeable} tradeable, logged track record")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            print(f"- {t.__name__}")
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
