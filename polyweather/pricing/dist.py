"""Probability distribution of the day's maximum temperature on a fixed
0.1 °C grid, with the operations the engine needs. Pure numpy."""
from __future__ import annotations

import math

import numpy as np


class TempDist:
    def __init__(self, grid: np.ndarray, p: np.ndarray):
        assert grid.shape == p.shape
        self.grid = grid
        self.p = np.clip(p, 0.0, None)
        s = self.p.sum()
        if s > 0:
            self.p = self.p / s

    # ---------- constructors ----------
    @classmethod
    def make_grid(cls, t_min: float, t_max: float, step: float) -> np.ndarray:
        n = int(round((t_max - t_min) / step)) + 1
        return np.round(t_min + np.arange(n) * step, 4)

    @classmethod
    def from_gaussian_mixture(cls, grid: np.ndarray, means: list[float],
                              sigmas: list[float], weights: list[float]) -> "TempDist":
        p = np.zeros_like(grid)
        wsum = sum(weights) or 1.0
        for m, s, w in zip(means, sigmas, weights):
            s = max(s, 0.05)
            p += (w / wsum) * np.exp(-0.5 * ((grid - m) / s) ** 2) / s
        return cls(grid, p)

    @classmethod
    def from_samples(cls, grid: np.ndarray, samples: np.ndarray,
                     bandwidth: float = 0.3) -> "TempDist":
        """KDE of empirical samples (used by the analog model)."""
        p = np.zeros_like(grid)
        if len(samples) == 0:
            return cls(grid, p)
        for x in samples:
            p += np.exp(-0.5 * ((grid - x) / bandwidth) ** 2)
        return cls(grid, p)

    @classmethod
    def point(cls, grid: np.ndarray, x: float) -> "TempDist":
        p = np.zeros_like(grid)
        p[int(np.argmin(np.abs(grid - x)))] = 1.0
        return cls(grid, p)

    # ---------- queries ----------
    def cdf_at(self, x: float) -> float:
        return float(self.p[self.grid <= x + 1e-9].sum())

    def exceedance(self, x: float) -> float:
        return 1.0 - self.cdf_at(x)

    def mean(self) -> float:
        return float((self.grid * self.p).sum())

    def quantile(self, q: float) -> float:
        c = np.cumsum(self.p)
        idx = int(np.searchsorted(c, q))
        return float(self.grid[min(idx, len(self.grid) - 1)])

    # ---------- transforms (each returns a new TempDist) ----------
    def blend(self, other: "TempDist", w_other: float) -> "TempDist":
        return TempDist(self.grid, (1 - w_other) * self.p + w_other * other.p)

    def shift(self, delta: float) -> "TempDist":
        k = int(round(delta / (self.grid[1] - self.grid[0])))
        if k == 0:
            return TempDist(self.grid, self.p.copy())
        p = np.zeros_like(self.p)
        if k > 0:
            p[k:] = self.p[:-k]
            p[-1] += self.p[-k:].sum()   # spill into top bin
        else:
            p[:k] = self.p[-k:]
            p[0] += self.p[:-k].sum()    # spill into bottom bin
        return TempDist(self.grid, p)

    def widen(self, factor: float) -> "TempDist":
        """Convolve with a Gaussian so total sigma grows by `factor` of current sigma."""
        if factor <= 1.0:
            return TempDist(self.grid, self.p.copy())
        mu = self.mean()
        var = float((((self.grid - mu) ** 2) * self.p).sum())
        extra = math.sqrt(max(var * (factor ** 2 - 1.0), 1e-6))
        step = self.grid[1] - self.grid[0]
        half = int(4 * extra / step) + 1
        ker_x = np.arange(-half, half + 1) * step
        ker = np.exp(-0.5 * (ker_x / extra) ** 2)
        ker /= ker.sum()
        p = np.convolve(self.p, ker, mode="same")
        return TempDist(self.grid, p)

    def floor_at(self, x: float) -> "TempDist":
        """Distribution of max(X, x): mass below x collapses onto x.
        This encodes 'the running max is already x'."""
        p = self.p.copy()
        below = self.grid < x - 1e-9
        mass = p[below].sum()
        p[below] = 0.0
        idx = int(np.argmin(np.abs(self.grid - x)))
        p[idx] += mass
        return TempDist(self.grid, p)

    def soft_cap(self, cap: float, strength: float, decay_c: float = 0.7) -> "TempDist":
        """With probability `strength` the physical cap binds: mass above `cap`
        decays exponentially (scale decay_c) and is moved to the cap bin."""
        strength = float(np.clip(strength, 0.0, 1.0))
        if strength <= 0:
            return TempDist(self.grid, self.p.copy())
        p = self.p.copy()
        above = self.grid > cap + 1e-9
        keep = np.ones_like(p)
        keep[above] = np.exp(-(self.grid[above] - cap) / max(decay_c, 0.05))
        capped = p * (keep * strength + (1 - strength))
        removed = p.sum() - capped.sum()
        idx = int(np.argmin(np.abs(self.grid - cap)))
        capped[idx] += removed
        return TempDist(self.grid, capped)

    def scale_exceedance(self, x: float, factor: float) -> "TempDist":
        """Multiply P(X > x) by `factor`, dumping removed mass onto x.
        Used by peak-passed: factor = 1 - p_peak_passed."""
        factor = float(np.clip(factor, 0.0, 1.0))
        p = self.p.copy()
        above = self.grid > x + 1e-9
        removed = p[above].sum() * (1 - factor)
        p[above] *= factor
        idx = int(np.argmin(np.abs(self.grid - x)))
        p[idx] += removed
        return TempDist(self.grid, p)

    # ---------- settlement ----------
    def integer_pmf(self, run_max_int: int | None = None,
                    sampling_deficit: float = 0.0) -> dict[int, float]:
        """P(settlement integer = j). Settlement = round-to-nearest of the
        (slightly undersampled) continuous max; readings are integer °C.
        Integers below the already-observed running max get zero and their
        mass moves to run_max_int."""
        x = self.grid - sampling_deficit
        lo = int(math.floor(x.min())) - 1
        hi = int(math.ceil(x.max())) + 1
        out: dict[int, float] = {}
        for j in range(lo, hi + 1):
            mask = (x >= j - 0.5) & (x < j + 0.5)
            m = float(self.p[mask].sum())
            if m > 1e-9:
                out[j] = m
        if run_max_int is not None:
            moved = 0.0
            for j in [k for k in out if k < run_max_int]:
                moved += out.pop(j)
            out[run_max_int] = out.get(run_max_int, 0.0) + moved
        s = sum(out.values()) or 1.0
        return {j: v / s for j, v in sorted(out.items())}
