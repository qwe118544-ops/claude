"""Offline tests for the trading engine + sizing (no network).

    PYTHONPATH=. python tests/test_engine.py
"""

from __future__ import annotations

import sys

from weather_alpha.engine import (EngineConfig, SimUniverse, SizingParams,
                                  TradingEngine, size_bet)
from weather_alpha.execsim import OrderType


def test_sizing_stays_in_band():
    p = SizingParams(min_bet=10, max_bet=30)
    for edge in (0.08, 0.12, 0.2, 0.5):
        for price in (0.1, 0.4, 0.6, 0.9):
            s = size_bet(0.5 + edge, price, edge, p)
            assert 10.0 <= s <= 30.0, (edge, price, s)
    # bigger edge -> not smaller bet
    assert size_bet(0.8, 0.5, 0.30, p) >= size_bet(0.6, 0.5, 0.10, p)
    print("  ok: bet size always within [10, 30], monotone-ish in edge")


def test_all_fills_within_bet_band():
    u = SimUniverse(n_markets=120, seed=11)
    eng = TradingEngine(u, EngineConfig(order_type=OrderType.FAK), seed=2)
    eng.run(140)
    sizes = [f["size"] for f in eng.fills]
    assert sizes, "expected some trades"
    assert all(10.0 <= s <= 30.0 for s in sizes), [s for s in sizes if not 10 <= s <= 30]
    print(f"  ok: {len(sizes)} bets, all sized in [10,30] "
          f"(min {min(sizes):.1f}, max {max(sizes):.1f})")


def test_positive_edge_shows_through_over_many_trades():
    u = SimUniverse(n_markets=400, seed=7)
    eng = TradingEngine(u, EngineConfig(order_type=OrderType.FAK, fee_rate=0.01), seed=3)
    eng.run(140)
    s = eng.broker.stats
    n = s.n_filled + s.n_partial
    assert n > 100, n
    assert s.net_pnl > 0, s.net_pnl                 # edge survives fees+slippage
    assert s.fees > 0                                # fees actually charged
    assert s.avg_slippage_per_share > 0             # latency cost realised
    assert abs(s.net_pnl - (s.gross_pnl - s.fees)) < 1e-6
    print(f"  ok: {n} trades, net P&L {s.net_pnl:+.0f}, "
          f"fees {s.fees:.1f}, slip/sh {s.avg_slippage_per_share:.4f}")


def test_snapshot_shape():
    u = SimUniverse(n_markets=20, seed=5)
    eng = TradingEngine(u, seed=1)
    eng.run(30)
    snap = eng.snapshot()
    for key in ("now", "tick", "account", "execution", "counts",
                "open_positions", "recent_signals", "recent_fills",
                "recent_settlements", "equity_curve"):
        assert key in snap, key
    for key in ("equity", "net_pnl", "gross_pnl", "fees", "unrealized"):
        assert key in snap["account"], key
    print("  ok: snapshot has all dashboard fields")


def test_no_double_trade_same_market():
    u = SimUniverse(n_markets=30, seed=9)
    eng = TradingEngine(u, seed=1)
    eng.run(140)
    # each market should appear at most once among opened positions
    traded = [f["market_id"] for f in eng.fills if f["status"] != "CANCELLED"]
    assert len(traded) == len(set(traded)), "a market was traded more than once"
    print(f"  ok: {len(set(traded))} distinct markets traded, no double-trades")


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
