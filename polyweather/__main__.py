"""CLI: polyweather run | doctor | backfill | settle | reprice"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="polyweather")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="start server + schedulers")
    run_p.add_argument("--host", default="127.0.0.1")
    run_p.add_argument("--port", type=int, default=8100)

    sub.add_parser("doctor", help="validate every data source live")

    bf = sub.add_parser("backfill", help="download history, build climatology")
    bf.add_argument("what", choices=["iem", "model-stats", "all"], nargs="?",
                    default="all")
    bf.add_argument("--years", type=int, default=12)
    bf.add_argument("--city", default=None)

    st = sub.add_parser("settle", help="settle+score a past day")
    st.add_argument("--date", required=False, help="YYYY-MM-DD (default: yesterday)")

    rp = sub.add_parser("reprice", help="one-shot: fetch everything once and price")
    rp.add_argument("--city", default=None)

    args = p.parse_args(argv)

    if args.cmd == "run":
        import uvicorn
        uvicorn.run("polyweather.api.app:app", host=args.host, port=args.port,
                    log_level="info")
    elif args.cmd == "doctor":
        from .doctor import run as doctor_run
        asyncio.run(doctor_run())
    elif args.cmd == "backfill":
        from .config import get_config
        cfg = get_config()
        if args.what in ("iem", "all"):
            from .backfill import iem
            iem.run(cfg, years=args.years, only_city=args.city)
        if args.what in ("model-stats", "all"):
            from .backfill import model_stats
            model_stats.run(cfg)
    elif args.cmd == "settle":
        from .config import get_config
        from .db import init_db
        from .evaluate import settle_city_day, settle_yesterday_all
        cfg = get_config()
        init_db()
        if args.date:
            for key in cfg.cities:
                print(settle_city_day(cfg, key, args.date))
        else:
            for r in settle_yesterday_all(cfg):
                print(r)
    elif args.cmd == "reprice":
        asyncio.run(_reprice_once(args.city))
    return 0


async def _reprice_once(only_city: str | None):
    """Single ingest+price cycle — useful for cron-less testing."""
    from .config import get_config
    from .db import init_db
    from .ingest.amedas import AmedasIngestor
    from .ingest.knmi import KnmiIngestor
    from .ingest.metar import MetarIngestor
    from .ingest.openmeteo import OpenMeteoIngestor
    from .ingest.polymarket import PolymarketIngestor
    from .pricing.engine import Engine

    cfg = get_config()
    init_db()
    for ing in (MetarIngestor(cfg), AmedasIngestor(cfg), KnmiIngestor(cfg),
                OpenMeteoIngestor(cfg), PolymarketIngestor(cfg)):
        try:
            changed = await ing.poll()
            print(f"{ing.name}: updated {changed}")
        except Exception as e:
            print(f"{ing.name}: {type(e).__name__}: {e}")
    engine = Engine(cfg)
    for key in cfg.cities:
        if only_city and key != only_city:
            continue
        out = engine.recompute(key)
        if out:
            top = sorted(out["int_pmf"].items(), key=lambda kv: -kv[1])[:4]
            print(f"{key} [{out['phase']}] run_max={out['run_max_int']} "
                  f"top: {[(k + '°C', round(v, 2)) for k, v in top]}")
        else:
            print(f"{key}: not enough data to price yet")


if __name__ == "__main__":
    sys.exit(main())
