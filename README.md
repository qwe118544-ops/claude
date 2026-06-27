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

## The live engine (the operating core)

The backtest answers "is there an edge?". The **live engine** (`weather_alpha.live`)
is the thing that actually runs: each pass it pulls current Polymarket weather
markets and prices, computes our probability from the **latest ensemble
forecast**, and flags markets where the two diverge enough to trade.

```bash
python -m weather_alpha.live --cities examples/cities.json \
    --min-edge 0.08 --min-liquidity 50 --max-hours 96 --log out/signals.jsonl
```

Each market becomes a `Signal`, tradeable only when **four conditions** hold at
once (this filter is what keeps you off noise):

1. **Edge** — `|my_prob − market_price| ≥ min_edge`
2. **Liquidity** — enough resting size at the price you'd actually take
3. **Timing** — settlement within a sensible window (not so far out the
   forecast hasn't converged; not already settling)
4. **Confidence** — the contract parsed confidently to (city, threshold, date)

Side selection: `my_prob` above the market → **BUY YES**; below → **BUY NO**.
Taker price comes from the order book (best ask / `1 − best bid`), not the mid.

**Why log every signal:** historical weather markets are too few for a strong
backtest, so the engine instead appends every evaluation to a JSONL track
record — `(timestamp, market, my_prob, market_price, edge, side, settle_date)`.
Once a market settles you fill in the outcome and measure **live Brier skill
vs the market** — the honest, forward-looking replacement for a thin backtest.

**Not included on purpose:** order placement, wallet/signing, and
correlation-aware position sizing. Execution touches real money and private
keys and is the most dangerous part — build it only after the logged signals
show a real live edge. The scanner deliberately stops at "here is the trade I
*would* make, and why."

> Verifiability: the live HTTP shapes (Open-Meteo ensemble, Polymarket Gamma +
> CLOB) could not be exercised in this repo's build environment because egress
> policy blocks those hosts. The **decision logic** — forecast→probability,
> edge, the four filters, side/taker-price selection, and the scanner with its
> logging — is fully unit-tested offline via injected fakes
> (`tests/test_live_signal.py`). Confirm the JSON field mappings in
> `live/polymarket.py` and `live/forecast.py` on the first networked run.

## Paper trading, the engine, and the dashboard

Beyond signalling, the repo includes a realistic **paper-execution simulator**,
a **continuous backend engine**, and a **read-only trading dashboard**. These
run end-to-end offline on a simulated market universe (live feeds are blocked
in this build environment), producing real fill/P&L/slippage numbers.

### Execution simulator (`weather_alpha.execsim`)

Models what actually erodes a backtested edge:

* fills walk the **order book level by level (VWAP)**, never at mid;
* **300–1000 ms latency** between signal and arrival, during which the book
  moves — so latency causes genuine adverse slippage, not a free fill;
* **FOK** (fill-or-kill) and **FAK** (fill-and-kill / IOC), partial fills, and
  cancel-on-insufficient-liquidity;
* **per-market dynamic fee** rates;
* exits matched against the **real bid side**, level by level;
* settlement to the binary outcome, with **net P&L, slippage, and unfilled
  rate** accounting.

### Engine (`weather_alpha.engine`)

The continuous backend: each `tick()` scans the whole universe, evaluates the
four-condition signal, **sizes each bet into the (10, 30) USDC band**
(edge-shaped, fractional-Kelly nudged, hard-clamped), routes it to the paper
broker, and settles resolved markets. It publishes a state snapshot; all
discovery/betting logic stays server-side.

Sanity check over a large simulated universe (400 markets, real fees+latency):
~300 trades, net P&L clearly positive after costs — i.e. when an edge exists,
the plumbing turns it into money. (The ROI in the sim is inflated by a large
injected mispricing; it validates the machinery, it is not a return forecast.)

### Dashboard (`weather_alpha.dashboard`)

```bash
python -m weather_alpha.dashboard --port 8787 --markets 60 --interval 0.5
# open http://127.0.0.1:8787
```

A self-contained (no-CDN) trading terminal that polls `/api/state`: equity +
P&L + fees + unrealized KPIs, an equity-curve sparkline, open positions
(shares/avg/mark/uPnL/hours-to-settle), recent fills (status/size/avg
price/slippage/latency/unfilled), settlements, and the live signal scan. It is
**pure display** — the engine in the background thread owns all logic.

### Execution is simulated only — NOT wired to real money

There is deliberately **no order placement, wallet, or signing**. Going from
this paper broker to real fills means adding key custody, real CLOB order
submission, and correlation-aware portfolio risk — the part that can actually
lose money and leak keys. Build it only after live, logged signals show a real
edge.

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
  cli.py          backtest command-line entry point
  live/           the operating core (live edge detection)
    markets.py      WeatherMarket model + best-effort question parser
    forecast.py     live ensemble -> P(max >= threshold) (member counting)
    signal.py       edge + four-condition filter + side/taker-price (pure logic)
    polymarket.py   live Gamma + CLOB client (verify field shapes on first run)
    scanner.py      one live pass + JSONL track-record logging
    cli.py          `python -m weather_alpha.live`
  execsim/        realistic paper-trading execution
    book.py         order book + level-by-level VWAP fill walk
    orders.py       Order/Fill/Position, FOK & FAK
    fees.py         per-market dynamic fee rate
    latency.py      300-1000 ms signal->order delay
    feed.py         book feed that evolves during latency (real slippage)
    broker.py       PaperBroker: delay->fill->partial/cancel->settle->stats
  engine/         continuous backend (discovery + betting, server-side)
    sizing.py       bet size clamped to (10, 30) USDC
    simfeed.py      offline simulated market universe
    engine.py       scan -> signal -> size -> route -> settle + snapshot
  dashboard/      read-only trading interface
    server.py       stdlib HTTP, /api/state + index.html (engine in a thread)
    index.html      self-contained trading terminal (no CDN)
tests/
  test_framework.py    backtest correctness (no network, no pytest)
  test_live_signal.py  live decision-logic correctness
  test_execsim.py      execution simulator correctness
  test_engine.py       engine + sizing correctness
examples/
  run_demo.sh
  cities.json
```
