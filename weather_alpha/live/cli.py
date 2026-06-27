"""CLI for the live scanner.

    python -m weather_alpha.live --min-edge 0.08 --log out/signals.jsonl

Requires reaching open-meteo.com and the Polymarket APIs. In egress-restricted
environments this will fail at the network step; the decision logic is covered
by the offline test suite (tests/test_live_signal.py).

City coordinates are supplied via a small JSON file (--cities) mapping a
lower-cased city name (as it appears in the question) to [lat, lon, tz]:

    {"new york": [40.78, -73.97, "America/New_York"],
     "chicago":  [41.88, -87.63, "America/Chicago"]}
"""

from __future__ import annotations

import argparse
import json
import sys

from .forecast import LiveEnsembleForecast
from .scanner import Scanner
from .signal import SignalParams


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weather_alpha.live",
        description="Scan live Polymarket weather markets for tradeable edges.",
    )
    p.add_argument("--cities", help="JSON file: {city: [lat, lon, tz]}", default=None)
    p.add_argument("--min-edge", type=float, default=0.08)
    p.add_argument("--min-liquidity", type=float, default=50.0)
    p.add_argument("--max-hours", type=float, default=96.0)
    p.add_argument("--models", default="gfs_seamless",
                   help="Open-Meteo ensemble models, comma-separated")
    p.add_argument("--log", default="out/signals.jsonl",
                   help="JSONL track-record log (append)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    cities = {}
    if args.cities:
        with open(args.cities, encoding="utf-8") as fh:
            cities = json.load(fh)

    try:
        from .polymarket import PolymarketClient
        client = PolymarketClient(city_coords=cities)
    except Exception as exc:  # pragma: no cover
        print(f"[error] could not init Polymarket client: {exc}", file=sys.stderr)
        return 2

    def forecast_factory(market):
        return LiveEnsembleForecast(
            latitude=market.latitude,
            longitude=market.longitude,
            timezone_name=market.timezone_name,
            models=args.models,
        )

    params = SignalParams(
        min_edge=args.min_edge,
        min_liquidity_usdc=args.min_liquidity,
        max_hours_to_settle=args.max_hours,
    )
    scanner = Scanner(client, forecast_factory, params=params, log_path=args.log)

    try:
        report = scanner.scan_once()
    except Exception as exc:
        print(f"[error] scan failed (often a blocked host or API shape change): {exc}",
              file=sys.stderr)
        print("[hint] the decision logic is verified offline via "
              "tests/test_live_signal.py; check network/API access for live runs.",
              file=sys.stderr)
        return 2

    print(report.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
