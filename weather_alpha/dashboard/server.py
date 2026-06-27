"""Stdlib HTTP server hosting the trading dashboard + a JSON state API.

A background thread ticks the engine; the HTTP handler serves the latest
snapshot. A lock keeps tick() and snapshot() from racing. No third-party deps.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from ..engine import EngineConfig, SimUniverse, TradingEngine

_HERE = os.path.dirname(__file__)


class DashboardServer:
    def __init__(self, n_markets: int = 60, tick_interval: float = 0.5,
                 seed: int = 7, config: Optional[EngineConfig] = None) -> None:
        universe = SimUniverse(n_markets=n_markets, seed=seed)
        self.engine = TradingEngine(universe, config or EngineConfig(fee_rate=0.01), seed=seed)
        self.tick_interval = tick_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- engine loop -------------------------------------------------- #
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                # when all markets resolve, recycle into a fresh universe
                if not self.engine.u.active():
                    seed = self.engine.tick_count + 1
                    self.engine = TradingEngine(
                        SimUniverse(n_markets=len(self.engine.u.markets), seed=seed),
                        self.engine.cfg, seed=seed)
                self.engine.tick()
            time.sleep(self.tick_interval)

    def start_engine(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def snapshot_json(self) -> bytes:
        with self._lock:
            return json.dumps(self.engine.snapshot()).encode("utf-8")

    # ---- http --------------------------------------------------------- #
    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/state"):
                    self._send(200, server.snapshot_json(), "application/json")
                elif self.path in ("/", "/index.html"):
                    with open(os.path.join(_HERE, "index.html"), "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")

        return Handler

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((host, port), self.make_handler())
        return httpd


def run(host: str = "127.0.0.1", port: int = 8787, n_markets: int = 60,
        tick_interval: float = 0.5, seed: int = 7) -> None:
    srv = DashboardServer(n_markets=n_markets, tick_interval=tick_interval, seed=seed)
    srv.start_engine()
    httpd = srv.serve(host, port)
    print(f"dashboard: http://{host}:{port}  (engine ticking every {tick_interval}s)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
