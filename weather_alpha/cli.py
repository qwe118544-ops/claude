"""Command-line entry point.

Examples
--------
Offline demo (no network needed) — proves the pipeline recovers calibration:
    python -m weather_alpha --source synthetic --threshold 85 --lead 1

Live (requires the host open-meteo.com to be reachable):
    python -m weather_alpha --source openmeteo \\
        --lat 40.78 --lon -73.97 --timezone America/New_York \\
        --threshold 90 --lead 1 --start 2024-06-01 --end 2024-08-31

With market prices to test the *real* edge:
    python -m weather_alpha --source openmeteo ... --market-csv prices.csv
    # prices.csv: columns  target_date,market_prob   (one implied prob per date)
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

from .backtest import run_backtest
from .data import OpenMeteoSource, SyntheticSource
from .evaluation import maybe_save_reliability_png


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weather_alpha",
        description="No-lookahead backtest + calibration scoring for threshold weather markets.",
    )
    p.add_argument("--source", choices=["synthetic", "openmeteo"], default="synthetic")
    p.add_argument("--threshold", type=float, required=True, help="temperature threshold (>=)")
    p.add_argument("--lead", type=int, default=1, help="forecast lead time in days (1..7)")
    p.add_argument("--train-frac", type=float, default=0.5)
    p.add_argument("--bins", type=int, default=10)
    p.add_argument("--outdir", default="out")
    p.add_argument("--market-csv", default=None,
                   help="optional CSV with columns target_date,market_prob")

    # openmeteo
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--timezone", default="auto")
    p.add_argument("--start", help="YYYY-MM-DD (openmeteo)")
    p.add_argument("--end", help="YYYY-MM-DD (openmeteo)")

    # synthetic
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--seed", type=int, default=7)
    return p


def _load_source(args):
    if args.source == "synthetic":
        return SyntheticSource(n_days=args.days, lead_days=args.lead, seed=args.seed)
    missing = [k for k in ("lat", "lon", "start", "end") if getattr(args, k) is None]
    if missing:
        raise SystemExit(f"--source openmeteo requires: {', '.join('--' + m for m in missing)}")
    return OpenMeteoSource(
        latitude=args.lat,
        longitude=args.lon,
        start_date=args.start,
        end_date=args.end,
        lead_days=args.lead,
        timezone=args.timezone,
    )


def _load_market(args, ftf) -> pd.Series | None:
    if not args.market_csv:
        return None
    mkt = pd.read_csv(args.market_csv)
    mkt["target_date"] = pd.to_datetime(mkt["target_date"])
    merged = pd.merge(
        ftf.df[["target_date"]], mkt[["target_date", "market_prob"]],
        on="target_date", how="left",
    )
    if merged["market_prob"].isna().any():
        n = int(merged["market_prob"].isna().sum())
        print(f"[warn] {n} dates had no market price; they will weaken the comparison.",
              file=sys.stderr)
    return merged["market_prob"]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    source = _load_source(args)

    try:
        ftf = source.fetch()
    except Exception as exc:  # network / API failures land here
        print(f"[error] data fetch failed: {exc}", file=sys.stderr)
        if args.source == "openmeteo":
            print("[hint] if the host is blocked by egress policy, validate the "
                  "pipeline offline with --source synthetic.", file=sys.stderr)
        return 2

    market_prob = _load_market(args, ftf)
    result = run_backtest(
        ftf, threshold=args.threshold, train_frac=args.train_frac,
        n_bins=args.bins, market_prob=market_prob,
    )

    print(result.summary())

    os.makedirs(args.outdir, exist_ok=True)
    pred_path = os.path.join(args.outdir, "predictions.csv")
    result.predictions.to_csv(pred_path, index=False)
    png_path = os.path.join(args.outdir, "reliability.png")
    saved = maybe_save_reliability_png(result.reliability, png_path)
    print(f"\nwrote {pred_path}" + (f" and {png_path}" if saved else
          " (install matplotlib for a reliability PNG)"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
