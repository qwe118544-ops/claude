# weather_alpha

A **no-lookahead backtest + calibration framework** for threshold weather
prediction markets (e.g. Polymarket *"NYC daily max temp ≥ 90°F?"*).

It does **not** place trades. It answers the only question worth answering
*before* you risk a cent:

> Under the contract's settlement definition, is my calibrated probability
> measurably better than the market price — and is it even better than just
> knowing the seasonal climatology?

This is the highest-leverage first piece of an automated weather-market
operation. Most people skip it, build a bot, and pay tuition to the market.
The edge in these markets lives in **calibration**, not in raw forecast
accuracy — so the first thing to build is the machine that *measures
calibration honestly*.

---

## Why this is the right first step

Three facts drive the whole design:

1. **You get paid for being better-calibrated than the market, not for being
   accurate in the abstract.** A merely-decent forecast that the market has
   mispriced beats a world-class forecast the market already agrees with.
2. **P&L is too noisy to learn from.** Hundreds of bets can't tell skill from
   luck. Proper scoring rules (Brier, log loss) and reliability diagrams can.
3. **Backtests lie when they peek at the future.** The single most common way
   a weather-market backtest fools its author is using a forecast (or a
   calibration fit) that wasn't available at decision time. This framework is
   built around *not* doing that.

---

## Install & run (offline, no network needed)

```bash
pip install -r requirements.txt        # numpy + pandas are the only hard deps

# Offline demo against a synthetic source with a KNOWN error model:
python -m weather_alpha --source synthetic --threshold 65 --lead 1

# Or the full demo + test suite:
bash examples/run_demo.sh
PYTHONPATH=. python tests/test_framework.py
```

Why a synthetic source? Because the right way to trust a backtest framework is
to feed it data whose answer you already know. The synthetic source draws a
seasonal + AR(1) "truth" and a forecast with a *known* lead-dependent bias and
spread. A correct framework must then (and does) recover near-perfect
calibration, beat climatology, and automatically widen its uncertainty as the
forecast lead grows. See the test suite for these assertions.

## Run against live data (Open-Meteo)

```bash
python -m weather_alpha --source openmeteo \
    --lat 40.78 --lon -73.97 --timezone America/New_York \
    --threshold 90 --lead 1 --start 2024-06-01 --end 2024-08-31
```

* **Truth** = ERA5 reanalysis daily max from the Open-Meteo archive API.
* **Forecast at lead L** = Open-Meteo *previous model runs*
  (`temperature_2m_max_previous_dayL`) — for each date, the value forecast L
  days earlier, i.e. genuinely no-lookahead.

> Network note: in egress-restricted environments the host `open-meteo.com`
> may be blocked by policy; the live source then fails loudly and the CLI
> points you back to `--source synthetic`. Nothing else needs the network.

## Test the *real* edge with market prices

Climatology skill is **necessary but not sufficient**. The number that
actually decides whether to trade is the **Brier skill score versus the market
price**. Supply historical prices and the backtest computes it:

```bash
python -m weather_alpha --source openmeteo ... --market-csv prices.csv
# prices.csv columns:  target_date,market_prob
#   (market_prob = the YES price / implied probability at the time you'd bet)
```

If `BSS vs MARKET` is not reliably **> 0** out-of-sample, you have no edge.

---

## What the output means

```
  BSS vs climatology      : +0.92    >0 means you beat "the seasonal base rate"
  Calibration error (ECE) : 0.026    count-weighted |predicted - observed|; lower better
  BSS vs MARKET           : +0.05    THE number — >0 means you beat the market price
  VERDICT: ...             one blunt line: edge plausible / no edge / not calibrated
```

Plus a reliability diagram (ASCII always; PNG if matplotlib is installed):
points on the diagonal = perfectly calibrated.

---

## How the no-lookahead guarantee is enforced

* **Forecast side:** a row's forecast for date *D* uses only the model run
  issued at *D − lead_days* (the synthetic source by construction; the live
  source via archived previous runs).
* **Calibration side:** EMOS bias/spread and the climatology base rate are fit
  on a strictly **earlier chronological train split**, then frozen and scored
  on the later test split. Splits are never random (random leaks the future).
  The test suite asserts `last_train_date < first_test_date`.

---

## The model (and where it sits on the real ladder)

The included calibrator is **EMOS-lite**: a linear bias correction
`mu = a + b·forecast` plus a learned Gaussian spread `sigma`, giving
`P(max ≥ t) = 1 − Φ((t − mu)/sigma)`. It is deliberately the *first rung*.

This framework is the measurement harness; here is the ladder it lets you climb
and verify, rung by rung, each change justified by a better out-of-sample score:

| Rung | Upgrade | Where it plugs in |
|---|---|---|
| 1 | Count ensemble members over threshold | `model.py` (swap point forecast → ensemble) |
| 2 | **Station bias correction / MOS** vs the exact settlement station | replace ERA5 truth with the ASOS/METAR station; per-station `a,b` |
| 3 | **Fix ensemble over-confidence** (EMOS/NGR, isotonic, quantile mapping) | `model.py` |
| 4 | **Multi-model blend** (ECMWF, GFS, ICON, GEM) weighted by skill | new source + ensembling in `model.py` |
| 5 | ML / AI weather models (GenCast, AIFS) post-processing | new source |
| 6 | **Intraday conditioning** — condition the daily-max distribution on observations already in, the most reliable late-day edge | new "day-of" model + source |

> The framework's job is to make every one of these an A/B test scored by
> Brier/BSS-vs-market, not a guess.

---

## Honest caveats — read before trusting any number

* **Settlement source ≠ ERA5.** Real contracts settle on a specific official
  station (often an ASOS/METAR site) with its own rounding and timing rules.
  ERA5 reanalysis is a stand-in for the demo; for live trading, replace the
  truth source with that exact station's official daily max. Getting the
  settlement definition wrong is the #1 way to lose with a "correct" model.
* **No real market prices ship here.** Without `--market-csv` the framework can
  only show skill vs climatology, which is necessary but not sufficient.
* **History window.** The standard Open-Meteo endpoint exposes ~92 past days
  and previous runs up to ~7 days. For multi-year backtests, point the source
  at the Historical Forecast API and extend previous-run handling.
* **This is not the trading system.** Sizing (correlation-aware fractional
  Kelly), execution (passive market-making in thin books), risk limits, and
  key security are deliberately out of scope until calibration is proven.

---

## Layout

```
weather_alpha/
  data.py         pluggable sources: SyntheticSource (offline) + OpenMeteoSource (live)
  model.py        EmosThresholdModel (calibration) + ClimatologyBaseline
  evaluation.py   Brier, log loss, BSS, reliability table/diagram
  backtest.py     no-lookahead chronological train/test orchestration + verdict
  cli.py          command-line entry point
tests/test_framework.py   correctness checks (no network, no pytest needed)
examples/run_demo.sh
```
