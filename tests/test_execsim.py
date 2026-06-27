"""Offline tests for the paper-trading execution simulator (no network).

    PYTHONPATH=. python tests/test_execsim.py

Checks the things that decide whether a paper edge is real: VWAP fills across
book levels, FOK/FAK semantics, partial fills, fees, latency-induced slippage,
settlement P&L (win and loss), and unfilled-rate accounting.
"""

from __future__ import annotations

import sys

from weather_alpha.execsim import (
    FeeModel, LatencyModel, Level, Order, OrderBook, OrderType, PaperBroker,
    Side, SimulatedBookFeed, fill_buy, fill_sell,
)


class FixedFeed:
    """Returns the same book at every time (isolates fill logic from latency)."""

    def __init__(self, book: OrderBook):
        self._book = book

    def book_at(self, token_id, t):
        return self._book


def _book(asks, bids=None):
    return OrderBook("tok", [Level(*b) for b in (bids or [])],
                     [Level(*a) for a in asks], ts=0.0)


def _zero_latency():
    return LatencyModel(0.0, 0.0)


def test_vwap_walk_buy():
    book = _book(asks=[(0.50, 10), (0.51, 10), (0.52, 10)])
    w = fill_buy(book, budget_usdc=10.0)
    assert w.fully_filled and abs(w.filled_notional - 10.0) < 1e-9
    assert 0.50 <= w.avg_price <= 0.51, w.avg_price
    print(f"  ok: buy VWAP {w.avg_price:.4f} for {w.filled_shares:.2f} shares")


def test_vwap_walk_sell():
    book = _book(asks=[], bids=[(0.50, 10), (0.49, 10)])
    w = fill_sell(book, shares_to_sell=15.0)
    # 10@0.50 + 5@0.49 = 5.0 + 2.45 = 7.45 over 15 shares -> 0.4967
    assert w.fully_filled and abs(w.filled_notional - 7.45) < 1e-9, w.filled_notional
    assert abs(w.avg_price - 7.45 / 15) < 1e-9
    print(f"  ok: sell VWAP {w.avg_price:.4f}")


def test_fok_cancels_when_insufficient_liquidity():
    feed = FixedFeed(_book(asks=[(0.50, 10), (0.51, 10)]))  # only ~10.1 USDC depth
    br = PaperBroker(feed, FeeModel(default_rate=0.0), _zero_latency())
    o = Order("m", "tok", Side.BUY_YES, OrderType.FOK, budget_usdc=25.0, ref_price=0.50)
    f = br.submit(o)
    assert f.status == "CANCELLED" and f.filled_shares == 0.0
    assert abs(f.unfilled_rate - 1.0) < 1e-9
    print("  ok: FOK cancels entirely on insufficient liquidity")


def test_fak_partial_fill():
    feed = FixedFeed(_book(asks=[(0.50, 10), (0.51, 10)]))  # ~10.1 USDC depth
    br = PaperBroker(feed, FeeModel(default_rate=0.0), _zero_latency())
    o = Order("m", "tok", Side.BUY_YES, OrderType.FAK, budget_usdc=25.0, ref_price=0.50)
    f = br.submit(o)
    assert f.status == "PARTIAL" and f.filled_shares > 0
    assert 0.0 < f.unfilled_rate < 1.0, f.unfilled_rate
    print(f"  ok: FAK partial fill, unfilled rate {f.unfilled_rate:.2%}")


def test_fees_applied():
    feed = FixedFeed(_book(asks=[(0.50, 100)]))
    br = PaperBroker(feed, FeeModel(default_rate=0.02), _zero_latency())
    f = br.submit(Order("m", "tok", Side.BUY_YES, OrderType.FAK, budget_usdc=10.0, ref_price=0.50))
    assert abs(f.fee - 0.02 * f.filled_notional) < 1e-9 and f.fee > 0
    print(f"  ok: fee {f.fee:.4f} on notional {f.filled_notional:.2f}")


def test_latency_causes_slippage():
    # Upward-drifting fair: by the time the (delayed) buy lands, asks are higher.
    feed = SimulatedBookFeed(vol_per_s=0.0, horizon_s=5, seed=1)
    feed.register("tok", fair0=0.50, drift_per_s=0.05)  # +0.05/s upward drift
    ref = feed.book_at("tok", 0.0).best_ask()
    br = PaperBroker(feed, FeeModel(default_rate=0.0), LatencyModel(800, 1000))
    f = br.submit(Order("m", "tok", Side.BUY_YES, OrderType.FAK, budget_usdc=20.0,
                        ref_price=ref, signal_time=0.0))
    assert f.slippage_per_share > 0, f.slippage_per_share  # paid worse than ref
    print(f"  ok: latency slippage {f.slippage_per_share:+.4f}/share "
          f"(ref {ref:.3f} -> vwap {f.avg_price:.3f})")


def test_settlement_win_and_loss_net_of_fees():
    feed = FixedFeed(_book(asks=[(0.50, 1000)]))
    br = PaperBroker(feed, FeeModel(default_rate=0.01), _zero_latency())
    f = br.submit(Order("m", "tok", Side.BUY_YES, OrderType.FAK, budget_usdc=25.0, ref_price=0.50))
    shares = f.filled_shares
    pnl = br.settle("m", "tok", event=True)
    # win: each share pays 1.0; bought at ~0.50 -> ~ +25 gross
    assert abs(pnl - (shares - 25.0)) < 1e-6
    assert abs(br.stats.net_pnl - (br.stats.gross_pnl - br.stats.fees)) < 1e-9
    assert br.stats.net_pnl > 0
    print(f"  ok: settle WIN gross {br.stats.gross_pnl:.2f}, "
          f"fees {br.stats.fees:.3f}, net {br.stats.net_pnl:.2f}")

    # A losing market.
    br2 = PaperBroker(feed, FeeModel(default_rate=0.01), _zero_latency())
    br2.submit(Order("m2", "tok", Side.BUY_YES, OrderType.FAK, budget_usdc=25.0, ref_price=0.50))
    loss = br2.settle("m2", "tok", event=False)
    assert abs(loss + 25.0) < 1e-6 and br2.stats.net_pnl < 0
    print(f"  ok: settle LOSS net {br2.stats.net_pnl:.2f}")


def test_buy_no_then_settle():
    feed = FixedFeed(_book(asks=[(0.40, 1000)]))  # NO token priced 0.40
    br = PaperBroker(feed, FeeModel(default_rate=0.0), _zero_latency())
    f = br.submit(Order("m", "no-tok", Side.BUY_NO, OrderType.FAK, budget_usdc=20.0, ref_price=0.40))
    # event did NOT happen -> NO wins, each share pays 1.0
    pnl = br.settle("m", "no-tok", event=False)
    assert abs(pnl - (f.filled_shares - 20.0)) < 1e-6 and pnl > 0
    print(f"  ok: BUY_NO wins when event false, net {br.stats.net_pnl:.2f}")


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
