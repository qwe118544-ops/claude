# polyweather

Intraday probabilistic pricing engine for Polymarket daily-high-temperature
markets. Cities: **London (EGLC)**, **Amsterdam (EHAM)**, **Tokyo (RJTT)** —
the exact stations these markets settle on (via the Wunderground history
table, which mirrors the airport METARs).

It does **not** trade. It outputs, per city per day, a live probability for
every integer-°C settlement value, with the reasons for every move. Polymarket
prices (top-2 buckets) are shown for reference only and never feed the model.

## What makes it price faster than the order book

- **10-minute local feeds** ahead of the half-hourly METAR: KNMI (Schiphol,
  same-site sensor) and JMA AMeDAS (Haneda). London has no public sub-METAR
  feed; EGLC runs on METAR.
- **Sea/estuary-breeze front tracker**: upstream sentinel stations
  (IJmuiden/Wijk aan Zee, Edogawa-rinkai/Chiba/Yokohama, Southend) are watched
  for the triad *wind veers onshore + dewpoint jumps + temp drops*. A
  confirmed front caps the temperature distribution 30–90 minutes before the
  station itself shows it.
- **Thermodynamic ceiling**: mixed-layer bound from 925/850 hPa model
  soundings — buckets above the physical ceiling are priced to ~0 regardless
  of what surface model output says.
- **Trajectory residual**: today's obs vs the model blend; models running
  hot/cold today shift the mean, a breaking script widens the distribution.
- **Model momentum**: consistent run-to-run revisions of hourly-cycling
  models (UKV / HARMONIE / MSM via Open-Meteo) shift the mean along the drift.
- **P(peak already passed)**: per-station, per-month climatology of
  peak-time; after the peak the distribution collapses onto the running max.
- **k-NN analog days**: 12 years of half-hourly history answers "from this
  exact situation, how much higher did the day end up?" — an evidence line
  fully independent of NWP.

Every pricing version is persisted with its full input snapshot (which METARs,
which model runs, which signals) — reproducible, and scored nightly against
the settlement (Brier/log-loss per hour checkpoint) at `/api/eval`.

## Setup

```bash
pip install -e .

# 1. validate every data source from your machine (do this first)
polyweather doctor

# 2. build climatology + analog matrices + model error stats (~10-20 min once)
polyweather backfill            # IEM history (12y) + Open-Meteo model stats

# 3. run
polyweather run --port 8100     # UI at http://127.0.0.1:8100
```

Optional but recommended (Amsterdam 10-min feed): get a free API key at
https://developer.dataplatform.knmi.nl/ and `export KNMI_API_KEY=...`.
Without it Amsterdam degrades to METAR-only and the doctor/health bar says so.

One-shot smoke test without the server: `polyweather reprice`.
Manual settle/score of a past day: `polyweather settle --date 2026-07-06`.

Live parsing tests against real endpoints (run on the deployment box):
`POLYWEATHER_LIVE=1 pytest tests/live -v`. Offline suite: `pytest`.

## Betting windows (local time) the state machine is built around

| City | Summer core window | Winter core window | Decisive early signal |
|---|---|---|---|
| Tokyo/Haneda (JST) | 11:30–14:30, peak often 12:30–13:30 | 12:00–14:30 | bay-breeze front at Edogawa-rinkai 11:00–13:00 |
| Amsterdam/Schiphol (CEST) | 13:00–17:00, peak 15:00–17:00 | 12:30–15:00 | coast wind shift at IJmuiden 11:00–14:00 |
| London City (BST) | 13:00–16:30, peak 15:00–16:30 | 11:30–14:30 | estuary easterly past Southend 12:00–15:00 |

Polling cadence follows these phases automatically (`scheduler.cadence` in
`config/cities.yaml`).

## Data sources (all free)

| Source | Cadence | Used for |
|---|---|---|
| aviationweather.gov METAR | 30 min + SPECI | settlement truth (EGLC/EHAM/RJTT) + London sentinels |
| JMA AMeDAS JSON | 10 min | Haneda fast feed + Tokyo-Bay sentinels |
| KNMI Data Platform 10-min | 10 min | Schiphol fast feed + North-Sea sentinels (needs free key) |
| Open-Meteo multi-model | hourly fetch | ECMWF/UKV/HARMONIE/AROME/ICON/MSM curves + 925/850 hPa ceiling |
| IEM ASOS archive | backfill | analog matrices, peak-time climatology |
| Open-Meteo historical forecast | backfill | per-model bias/σ calibration |
| Polymarket Gamma API | minutes | top-2 bucket display only |

## Known limits (deliberate v1 scope)

- EGLC has no public 10-minute feed; London prices on half-hourly METARs.
  EGLC also stops reporting overnight and Saturday afternoon–Sunday midday —
  settlement only sees published METARs; the running max handles this
  correctly but be aware on winter Saturdays.
- Model bias/σ start from the day-0 calibration in `backfill model-stats`;
  they sharpen automatically as your own fetch history accumulates
  (visible at `/api/eval`).
- Verify the exact resolution station in each market's rules text before
  trading a new series — same city, different series can use another airport.

## Layout

```
config/cities.yaml        all local knowledge: stations, sentinels, sectors, windows
polyweather/ingest/       metar, amedas, knmi, openmeteo, polymarket collectors
polyweather/signals/      breeze_front, ceiling, residual, momentum, peak_passed
polyweather/pricing/      dist (PMF ops), engine, analog kNN, bucket mapping
polyweather/backfill/     IEM history, model error stats
polyweather/evaluate.py   nightly settlement + scoring
polyweather/doctor.py     live validation of every source
polyweather/api/ + web/   FastAPI + SSE + single-page UI (vendored ECharts)
```
