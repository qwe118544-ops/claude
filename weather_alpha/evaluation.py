"""Proper scoring rules and reliability — the only honest measure of edge.

We never judge a probabilistic forecaster by realized P&L (too noisy to learn
from in hundreds of bets). We judge it by calibration and sharpness via:

    * Brier score        mean((p - y)^2)      lower is better
    * Log loss           mean(-[y log p + ...]) lower is better
    * Brier skill score  1 - BS/BS_ref         >0 means "beats the reference"
    * Reliability table   binned predicted prob vs observed frequency

The reference for the skill score is climatology. The most important number in
real use is the skill score of your model *against the market price* — if it is
not reliably > 0, you have no edge and should not trade.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-6) -> float:
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier_skill_score(p: np.ndarray, y: np.ndarray, p_ref: np.ndarray) -> float:
    """1 - BS(model)/BS(reference). >0: model beats reference."""
    bs = brier_score(p, y)
    bs_ref = brier_score(p_ref, y)
    if bs_ref <= 0:
        return float("nan")
    return float(1.0 - bs / bs_ref)


@dataclass
class ReliabilityTable:
    table: pd.DataFrame  # bin_lo, bin_hi, mean_pred, obs_freq, count

    def calibration_error(self) -> float:
        """Count-weighted mean |predicted - observed| (a.k.a. ECE)."""
        t = self.table.dropna(subset=["obs_freq"])
        if t["count"].sum() == 0:
            return float("nan")
        w = t["count"] / t["count"].sum()
        return float(np.sum(w * np.abs(t["mean_pred"] - t["obs_freq"])))


def reliability_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> ReliabilityTable:
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges, right=False) - 1, 0, n_bins - 1)

    rows = []
    for b in range(n_bins):
        mask = idx == b
        count = int(mask.sum())
        rows.append(
            {
                "bin_lo": edges[b],
                "bin_hi": edges[b + 1],
                "mean_pred": float(p[mask].mean()) if count else np.nan,
                "obs_freq": float(y[mask].mean()) if count else np.nan,
                "count": count,
            }
        )
    return ReliabilityTable(pd.DataFrame(rows))


def reliability_diagram_text(rt: ReliabilityTable, width: int = 40) -> str:
    """ASCII reliability diagram: 'P' predicted vs 'O' observed per bin."""
    lines = ["  bin        pred   obs    n   | 0" + " " * (width - 4) + "1"]
    for _, r in rt.table.iterrows():
        if r["count"] == 0:
            continue
        lo, hi = r["bin_lo"], r["bin_hi"]
        pred, obs, n = r["mean_pred"], r["obs_freq"], int(r["count"])
        bar = [" "] * (width + 1)
        bar[int(round(pred * width))] = "P"
        op = int(round(obs * width))
        bar[op] = "O" if bar[op] == " " else "X"  # X = perfectly calibrated bin
        lines.append(
            f"[{lo:.1f},{hi:.1f})  {pred:5.2f}  {obs:5.2f}  {n:3d}  |{''.join(bar)}|"
        )
    lines.append("  (P=mean predicted, O=observed freq, X=they coincide)")
    return "\n".join(lines)


def maybe_save_reliability_png(rt: ReliabilityTable, path: str) -> bool:
    """Save a reliability diagram PNG if matplotlib is available. Returns success."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False

    t = rt.table.dropna(subset=["obs_freq"])
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "--", color="grey", label="perfect calibration")
    ax.scatter(t["mean_pred"], t["obs_freq"], s=t["count"] * 3 + 10, label="model")
    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title("Reliability diagram")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True
