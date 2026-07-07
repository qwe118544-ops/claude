import numpy as np

from polyweather.pricing.dist import TempDist

GRID = TempDist.make_grid(-15, 50, 0.1)


def test_mixture_normalized():
    d = TempDist.from_gaussian_mixture(GRID, [25.0, 27.0], [1.0, 0.8], [1, 1])
    assert abs(d.p.sum() - 1.0) < 1e-9
    assert 24.5 < d.mean() < 27.5


def test_floor_at_collapses_lower_mass():
    d = TempDist.from_gaussian_mixture(GRID, [25.0], [1.0], [1])
    f = d.floor_at(26.0)
    assert f.cdf_at(25.9) < 1e-6
    # all mass at or above 26
    assert abs(f.exceedance(25.95) - 1.0) < 1e-6
    assert abs(f.p.sum() - 1.0) < 1e-9


def test_soft_cap_moves_mass_down():
    d = TempDist.from_gaussian_mixture(GRID, [30.0], [1.5], [1])
    before = d.exceedance(31.0)
    c = d.soft_cap(29.0, strength=0.9)
    after = c.exceedance(31.0)
    assert after < before * 0.5
    assert abs(c.p.sum() - 1.0) < 1e-9


def test_soft_cap_zero_strength_noop():
    d = TempDist.from_gaussian_mixture(GRID, [30.0], [1.5], [1])
    c = d.soft_cap(29.0, strength=0.0)
    assert np.allclose(c.p, d.p)


def test_scale_exceedance():
    d = TempDist.from_gaussian_mixture(GRID, [25.0], [1.0], [1])
    x = 25.0
    e0 = d.exceedance(x)
    s = d.scale_exceedance(x, 0.2)  # peak passed with p=0.8
    assert abs(s.exceedance(x) - e0 * 0.2) < 1e-6
    assert abs(s.p.sum() - 1.0) < 1e-9


def test_integer_pmf_run_max_floor():
    d = TempDist.from_gaussian_mixture(GRID, [25.0], [1.5], [1])
    pmf = d.integer_pmf(run_max_int=26)
    assert min(pmf) == 26
    assert abs(sum(pmf.values()) - 1.0) < 1e-6
    assert pmf[26] > 0.5  # collapsed mass sits on the floor


def test_integer_pmf_rounding_window():
    d = TempDist.point(GRID, 25.4)
    pmf = d.integer_pmf()
    assert pmf.get(25, 0) > 0.99
    d = TempDist.point(GRID, 25.6)
    pmf = d.integer_pmf()
    assert pmf.get(26, 0) > 0.99


def test_sampling_deficit_shifts_down():
    d = TempDist.point(GRID, 25.55)
    pmf = d.integer_pmf(sampling_deficit=0.15)
    assert pmf.get(25, 0) > 0.99  # 25.55 - 0.15 = 25.4 -> 25


def test_widen_increases_spread():
    d = TempDist.from_gaussian_mixture(GRID, [25.0], [0.8], [1])
    w = d.widen(1.5)
    def var(x):
        mu = x.mean()
        return float((((x.grid - mu) ** 2) * x.p).sum())
    assert var(w) > var(d) * 1.8
