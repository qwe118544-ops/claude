"""Correctness checks for the calibration backtest framework.

These run without network access (synthetic source only) and without pytest:

    python tests/test_framework.py

The synthetic source has a *known* error model, so a correct framework must
(a) recover good calibration, (b) beat climatology, (c) learn that longer
leads are more uncertain, and (d) score a supplied market price correctly.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from weather_alpha import SyntheticSource, run_backtest
from weather_alpha.evaluation import brier_score, brier_skill_score


def _run(threshold=65.0, lead=1, days=600, seed=7, market_prob=None):
    ftf = SyntheticSource(n_days=days, lead_days=lead, seed=seed).fetch()
    return ftf, run_backtest(ftf, threshold=threshold, market_prob=market_prob)


def test_beats_climatology_and_is_calibrated():
    _, res = _run()
    assert res.bss_vs_clim > 0.3, res.bss_vs_clim
    assert res.calibration_error < 0.08, res.calibration_error
    print(f"  ok: BSS vs clim = {res.bss_vs_clim:+.3f}, ECE = {res.calibration_error:.3f}")


def test_skill_degrades_with_lead():
    bss = {L: _run(lead=L)[1].bss_vs_clim for L in (1, 3, 5)}
    sig = {L: _run(lead=L)[1].emos_sigma for L in (1, 3, 5)}
    assert bss[1] > bss[3] > bss[5], bss
    assert sig[1] < sig[3] < sig[5], sig
    print(f"  ok: BSS {bss[1]:.3f}>{bss[3]:.3f}>{bss[5]:.3f}; "
          f"sigma {sig[1]:.2f}<{sig[3]:.2f}<{sig[5]:.2f}")


def test_no_lookahead_split_is_chronological():
    ftf, res = _run()
    # The last train date must be strictly before the first test date.
    dates = ftf.df["target_date"].to_numpy()
    last_train = dates[res.n_train - 1]
    first_test = dates[res.n_train]
    assert last_train < first_test
    print(f"  ok: last train {pd.Timestamp(last_train).date()} < "
          f"first test {pd.Timestamp(first_test).date()}")


def test_model_beats_a_deliberately_bad_market():
    # Build a "market" = climatology constant (deliberately uninformative).
    ftf, base = _run()
    y_rate = float((ftf.df["truth_max"] >= 65.0).mean())
    bad_market = pd.Series(np.full(len(ftf.df), y_rate))
    _, res = _run(market_prob=bad_market)
    assert res.bss_vs_market is not None
    assert res.bss_vs_market > 0.0, res.bss_vs_market
    print(f"  ok: BSS vs (bad) market = {res.bss_vs_market:+.3f} > 0")


def test_model_cannot_beat_a_near_perfect_market():
    # A market that already knows the EMOS probabilities should NOT be beatable.
    ftf, base = _run()
    near_perfect = base.predictions.set_index("target_date")["p_model"]
    market = pd.Series(
        pd.merge(ftf.df[["target_date"]], near_perfect.rename("m"),
                 on="target_date", how="left")["m"].fillna(0.5).to_numpy()
    )
    _, res = _run(market_prob=market)
    # On the test portion the market == model, so skill should be ~0 (not positive).
    assert res.bss_vs_market is None or res.bss_vs_market <= 0.05, res.bss_vs_market
    print(f"  ok: BSS vs near-perfect market = {res.bss_vs_market:+.3f} (~0, not positive)")


def test_scoring_rules_basic():
    y = np.array([1.0, 0.0, 1.0, 0.0])
    perfect = np.array([1.0, 0.0, 1.0, 0.0])
    assert brier_score(perfect, y) == 0.0
    assert brier_skill_score(perfect, y, np.full(4, 0.5)) == 1.0
    print("  ok: scoring rules sane")


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
