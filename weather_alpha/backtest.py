"""No-lookahead backtest orchestration.

Pipeline:
    1. Sort rows by target_date.
    2. Chronological split: earliest `train_frac` -> train, the rest -> test.
       (Chronological, never random — random splits leak the future.)
    3. Fit EMOS + climatology on TRAIN only.
    4. Predict on TEST.
    5. Score with proper scoring rules + reliability; compare to climatology
       and, if supplied, to the market price.

The result object carries every number you need to decide "is there an edge?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .data import ForecastTruthFrame
from .evaluation import (
    ReliabilityTable,
    brier_score,
    brier_skill_score,
    log_loss,
    reliability_diagram_text,
    reliability_table,
)
from .model import ClimatologyBaseline, EmosThresholdModel


@dataclass
class BacktestResult:
    threshold: float
    lead_days: int
    n_train: int
    n_test: int
    base_rate_test: float

    model_brier: float
    clim_brier: float
    model_log_loss: float
    bss_vs_clim: float
    calibration_error: float

    market_brier: Optional[float] = None
    bss_vs_market: Optional[float] = None

    emos_a: float = 0.0
    emos_b: float = 1.0
    emos_sigma: float = 0.0

    reliability: Optional[ReliabilityTable] = None
    predictions: Optional[pd.DataFrame] = None
    notes: list = field(default_factory=list)

    def verdict(self) -> str:
        """A blunt one-line read on whether an edge is plausible."""
        if self.bss_vs_market is not None:
            if self.bss_vs_market > 0.02:
                return ("EDGE PLAUSIBLE: model beats the market price out-of-sample "
                        f"(BSS vs market = {self.bss_vs_market:+.3f}).")
            return ("NO EDGE vs MARKET: model does not reliably beat the market price "
                    f"(BSS vs market = {self.bss_vs_market:+.3f}). Do not trade on this.")
        # No market prices supplied -> can only judge vs climatology.
        if self.bss_vs_clim > 0.05 and self.calibration_error < 0.08:
            return ("Skill vs climatology and well calibrated "
                    f"(BSS={self.bss_vs_clim:+.3f}, ECE={self.calibration_error:.3f}). "
                    "Necessary but NOT sufficient — supply market prices to test the real edge.")
        return (f"Weak/!calibrated vs climatology (BSS={self.bss_vs_clim:+.3f}, "
                f"ECE={self.calibration_error:.3f}).")

    def summary(self) -> str:
        lines = [
            "=" * 64,
            f"  Backtest: max-temp >= {self.threshold}  |  lead = {self.lead_days}d",
            "=" * 64,
            f"  train / test points     : {self.n_train} / {self.n_test}",
            f"  test base rate (event)  : {self.base_rate_test:.3f}",
            f"  EMOS fit                : mu = {self.emos_a:+.2f} + {self.emos_b:.2f}*fcst, "
            f"sigma = {self.emos_sigma:.2f}",
            "-" * 64,
            f"  Brier (model)           : {self.model_brier:.4f}",
            f"  Brier (climatology)     : {self.clim_brier:.4f}",
            f"  Log loss (model)        : {self.model_log_loss:.4f}",
            f"  BSS vs climatology      : {self.bss_vs_clim:+.4f}   (>0 beats climatology)",
            f"  Calibration error (ECE) : {self.calibration_error:.4f}   (lower is better)",
        ]
        if self.market_brier is not None:
            lines += [
                "-" * 64,
                f"  Brier (market price)    : {self.market_brier:.4f}",
                f"  BSS vs MARKET           : {self.bss_vs_market:+.4f}   (THE number that matters)",
            ]
        lines += ["-" * 64, "  VERDICT: " + self.verdict()]
        if self.reliability is not None:
            lines += ["", reliability_diagram_text(self.reliability)]
        if self.notes:
            lines += ["", "  notes:"] + [f"   - {n}" for n in self.notes]
        lines.append("=" * 64)
        return "\n".join(lines)


def run_backtest(
    ftf: ForecastTruthFrame,
    threshold: float,
    train_frac: float = 0.5,
    n_bins: int = 10,
    market_prob: Optional[pd.Series] = None,
) -> BacktestResult:
    """Run the no-lookahead calibration backtest.

    `market_prob`, if given, must be a Series indexed identically to ftf.df
    (one implied probability per row) representing the market's price at the
    time the bet would have been placed.
    """
    df = ftf.df.reset_index(drop=True).copy()
    df["y"] = (df["truth_max"] >= threshold).astype(float)
    if market_prob is not None:
        df["market_prob"] = np.asarray(market_prob, dtype=float)

    n = len(df)
    n_train = int(round(n * train_frac))
    if n_train < 10 or n - n_train < 5:
        raise ValueError(
            f"not enough data: {n} rows -> train {n_train}, test {n - n_train}. "
            "Need >=10 train and >=5 test."
        )

    train, test = df.iloc[:n_train], df.iloc[n_train:]
    notes = []

    # Degenerate-threshold guards: a base rate of ~0 or ~1 makes scores meaningless.
    train_rate = float((train["truth_max"] >= threshold).mean())
    if train_rate < 0.02 or train_rate > 0.98:
        notes.append(
            f"threshold is near-degenerate in train (base rate {train_rate:.2f}); "
            "pick a threshold nearer the seasonal median for a meaningful test."
        )

    emos = EmosThresholdModel().fit(train["forecast_max"].to_numpy(), train["truth_max"].to_numpy())
    clim = ClimatologyBaseline().fit(train["truth_max"].to_numpy(), threshold)

    y = test["y"].to_numpy()
    p_model = emos.predict_proba_ge(test["forecast_max"].to_numpy(), threshold)
    p_clim = clim.predict_proba_ge(len(test))

    rt = reliability_table(p_model, y, n_bins=n_bins)

    preds = test[["target_date", "forecast_max", "truth_max", "y"]].copy()
    preds["p_model"] = p_model
    preds["p_clim"] = p_clim

    market_brier = bss_vs_market = None
    if "market_prob" in test.columns:
        p_mkt = test["market_prob"].to_numpy()
        preds["p_market"] = p_mkt
        market_brier = brier_score(p_mkt, y)
        bss_vs_market = brier_skill_score(p_model, y, p_mkt)

    return BacktestResult(
        threshold=threshold,
        lead_days=int(df["lead_days"].iloc[0]),
        n_train=n_train,
        n_test=len(test),
        base_rate_test=float(y.mean()),
        model_brier=brier_score(p_model, y),
        clim_brier=brier_score(p_clim, y),
        model_log_loss=log_loss(p_model, y),
        bss_vs_clim=brier_skill_score(p_model, y, p_clim),
        calibration_error=rt.calibration_error(),
        market_brier=market_brier,
        bss_vs_market=bss_vs_market,
        emos_a=emos.a,
        emos_b=emos.b,
        emos_sigma=emos.sigma,
        reliability=rt,
        predictions=preds.reset_index(drop=True),
        notes=notes,
    )
