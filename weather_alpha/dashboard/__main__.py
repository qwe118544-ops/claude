import argparse

from .server import run


def main() -> int:
    p = argparse.ArgumentParser(prog="weather_alpha.dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--markets", type=int, default=60)
    p.add_argument("--interval", type=float, default=0.5, help="engine tick seconds")
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    run(host=a.host, port=a.port, n_markets=a.markets, tick_interval=a.interval, seed=a.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
