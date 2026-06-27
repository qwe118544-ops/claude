"""Scanner: one pass of the live loop, with a track-record log.

Steps per pass:
    1. client.list_weather_markets()           discover current weather markets
    2. client.fill_book(market)                read live price + order book
    3. forecast.probability(...)               compute our probability
    4. evaluate_signal(market, my_prob, ...)   apply the four-condition filter
    5. append every evaluation to a JSONL log  -> the live track record

The log is the point: each line records (timestamp, market, my_prob,
market_price, edge, side, tradeable, settlement date). Once a market settles
you can fill in the outcome and measure our live Brier skill vs the market —
the honest replacement for a thin historical backtest.

The Scanner depends only on the `MarketClient` protocol and a forecast object,
so it is fully testable offline with fakes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from .markets import WeatherMarket
from .signal import Signal, SignalParams, evaluate_signal


@runtime_checkable
class MarketClient(Protocol):
    def list_weather_markets(self, limit: int = 200) -> list[WeatherMarket]: ...
    def fill_book(self, market: WeatherMarket) -> WeatherMarket: ...


class ForecastProvider(Protocol):
    def probability(self, target, threshold: float, direction: str = ">=",
                    bias_correction: float = 0.0) -> float: ...


@dataclass
class ScanReport:
    signals: list[Signal]
    n_markets: int
    n_tradeable: int
    log_path: Optional[str]

    def summary(self) -> str:
        lines = [f"scanned {self.n_markets} weather markets -> "
                 f"{self.n_tradeable} tradeable signal(s)"]
        for s in self.signals:
            tag = "** TRADE **" if s.tradeable else "          "
            lines.append(
                f"{tag} {s.side or '-':8s} edge {s.edge:+.3f}  "
                f"my {s.my_prob:.3f} / mkt {s.market_prob:.3f}  | {s.question[:60]}")
            if s.tradeable:
                lines.append(f"            -> {s.reasons[-1]}")
        if self.log_path:
            lines.append(f"logged {len(self.signals)} evaluations to {self.log_path}")
        return "\n".join(lines)


class Scanner:
    def __init__(
        self,
        client: MarketClient,
        forecast_factory,
        params: Optional[SignalParams] = None,
        log_path: Optional[str] = None,
    ) -> None:
        # forecast_factory(market) -> object with .probability(...); lets the
        # scanner build a per-location forecaster (or inject a fake in tests).
        self.client = client
        self.forecast_factory = forecast_factory
        self.params = params or SignalParams()
        self.log_path = log_path

    def scan_once(self, now: Optional[datetime] = None) -> ScanReport:
        now = now or datetime.now(timezone.utc)
        markets = self.client.list_weather_markets()
        signals: list[Signal] = []

        for market in markets:
            try:
                self.client.fill_book(market)
            except Exception as exc:  # one bad market must not kill the pass
                market.notes.append(f"book fetch failed: {exc}")

            if not market.is_tradeable_contract():
                # Still evaluate (records why it was skipped), with prob=nan.
                signals.append(evaluate_signal(market, float("nan"), self.params, now))
                continue

            try:
                fc = self.forecast_factory(market)
                my_prob = fc.probability(
                    market.settle_date, market.threshold_f,
                    direction=market.direction)
            except Exception as exc:
                market.notes.append(f"forecast failed: {exc}")
                signals.append(evaluate_signal(market, float("nan"), self.params, now))
                continue

            signals.append(evaluate_signal(market, my_prob, self.params, now))

        self._log(signals, now)
        n_trade = sum(1 for s in signals if s.tradeable)
        return ScanReport(signals, len(markets), n_trade, self.log_path)

    def _log(self, signals, now: datetime) -> None:
        if not self.log_path:
            return
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            for s in signals:
                rec = {"ts": now.isoformat(), **s.as_dict(), "outcome": None}
                fh.write(json.dumps(rec) + "\n")
