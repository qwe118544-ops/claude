"""The continuous trading engine: scan -> signal -> size -> route -> settle.

One `tick()` is one full pass over the whole market universe. The engine keeps
a published state snapshot (account, exec stats, open positions, recent
signals/fills/settlements, equity curve) that the dashboard renders read-only.

Runs on the SimUniverse offline; the same loop would run on live clients by
swapping the universe + book feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..execsim import (FeeModel, LatencyModel, Order, OrderType, PaperBroker,
                       Side, SimulatedBookFeed)
from ..live.markets import WeatherMarket
from ..live.signal import SignalParams, evaluate_signal
from .simfeed import SimMarket, SimUniverse
from .sizing import SizingParams, size_bet

_TICK = 0.01  # book tick size used to derive top-of-book from a fair price


@dataclass
class EngineConfig:
    starting_cash: float = 500.0
    order_type: OrderType = OrderType.FAK
    signal: SignalParams = field(default_factory=SignalParams)
    sizing: SizingParams = field(default_factory=SizingParams)
    fee_rate: float = 0.0
    latency_min_ms: float = 300.0
    latency_max_ms: float = 1000.0
    max_history: int = 50


class TradingEngine:
    def __init__(self, universe: SimUniverse, config: Optional[EngineConfig] = None,
                 seed: int = 0) -> None:
        self.u = universe
        self.cfg = config or EngineConfig()
        self.feed = SimulatedBookFeed(tick=_TICK, seed=seed)
        self.broker = PaperBroker(
            self.feed,
            FeeModel(default_rate=self.cfg.fee_rate),
            LatencyModel(self.cfg.latency_min_ms, self.cfg.latency_max_ms),
        )
        self._by_id: dict[str, SimMarket] = {m.market_id: m for m in universe.markets}
        self._traded: set[str] = set()       # markets we already hold
        self._settled: set[str] = set()
        self.tick_count = 0
        self.signals: list = []
        self.fills: list = []
        self.settlements: list = []
        self.equity_curve: list = []

    # ------------------------------------------------------------------ #
    def _weather_market(self, m: SimMarket) -> WeatherMarket:
        fair = m.market_yes_fair
        ask = min(0.999, fair + _TICK * 0.5)
        bid = max(0.001, fair - _TICK * 0.5)
        # liquidity estimate near top of book, in USDC notional
        liq = m.depth_shares * fair * 4.0
        return WeatherMarket(
            market_id=m.market_id, question=m.question, yes_token_id=m.yes_token_id,
            city=m.city, latitude=m.latitude, longitude=m.longitude,
            threshold_f=m.threshold_f, direction=">=", settle_date=m.settle_dt.date(),
            parse_confident=True, yes_price=fair, best_bid=bid, best_ask=ask,
            top_ask_size=liq, top_bid_size=liq, settle_dt_utc=m.settle_dt,
        )

    def _route(self, m: SimMarket, signal) -> None:
        is_yes = signal.side == "BUY_YES"
        token = m.yes_token_id if is_yes else m.no_token_id
        token_fair = m.market_yes_fair if is_yes else (1.0 - m.market_yes_fair)
        ref = min(0.999, token_fair + _TICK * 0.5)
        my_p = m.model_prob if is_yes else (1.0 - m.model_prob)
        size = size_bet(my_p, ref, signal.edge, self.cfg.sizing)

        # Build a fresh short-horizon book path for this order (fair=token_fair).
        self.feed.register(token, fair0=token_fair,
                           drift_per_s=0.0, depth_shares=m.depth_shares)
        order = Order(
            market_id=m.market_id, token_id=token,
            side=Side.BUY_YES if is_yes else Side.BUY_NO,
            order_type=self.cfg.order_type, budget_usdc=size, ref_price=ref,
            signal_time=0.0,
        )
        fill = self.broker.submit(order)
        # tag the position with the question for the dashboard
        pos = self.broker.positions.get((m.market_id, token))
        if pos is not None and not pos.question:
            pos.question = m.question
        if fill.filled_shares > 0:
            self._traded.add(m.market_id)
        self._push(self.fills, {
            "market_id": m.market_id, "city": m.city, "side": signal.side,
            "status": fill.status, "size": round(size, 2),
            "filled_notional": round(fill.filled_notional, 2),
            "avg_price": round(fill.avg_price, 4),
            "slippage": round(fill.slippage_per_share, 5),
            "latency_ms": round(fill.latency_ms, 0),
            "unfilled_rate": round(fill.unfilled_rate, 3),
        })

    def _settle_due(self) -> None:
        for m in self.u.markets:
            if m.resolved and m.market_id not in self._settled:
                for token in (m.yes_token_id, m.no_token_id):
                    pos = self.broker.positions.get((m.market_id, token))
                    if pos and not pos.settled and pos.shares > 1e-9:
                        pnl = self.broker.settle(m.market_id, token, bool(m.event_outcome))
                        self._push(self.settlements, {
                            "market_id": m.market_id, "city": m.city,
                            "side": pos.side.value, "outcome": bool(m.event_outcome),
                            "pnl": round(pnl, 2),
                        })
                self._settled.add(m.market_id)

    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        self.u.step()
        self.tick_count += 1
        for m in self.u.active():
            wm = self._weather_market(m)
            sig = evaluate_signal(wm, my_prob=m.model_prob, params=self.cfg.signal,
                                  now=self.u.now)
            self._push(self.signals, {
                "market_id": m.market_id, "city": m.city,
                "question": m.question, "my_prob": round(m.model_prob, 3),
                "market_prob": round(m.market_yes_fair, 3),
                "edge": round(sig.edge, 3), "side": sig.side,
                "tradeable": sig.tradeable,
            })
            if sig.tradeable and m.market_id not in self._traded:
                self._route(m, sig)
        self._settle_due()
        self.equity_curve.append(round(self.equity(), 2))

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.tick()

    # ------------------------------------------------------------------ #
    def unrealized(self) -> float:
        total = 0.0
        for (mid, token), pos in self.broker.positions.items():
            if pos.settled or pos.shares <= 1e-9:
                continue
            m = self._by_id.get(mid)
            if not m:
                continue
            mark = m.market_yes_fair if pos.side == Side.BUY_YES else (1 - m.market_yes_fair)
            total += pos.shares * mark - pos.cost_basis
        return total

    def equity(self) -> float:
        return self.cfg.starting_cash + self.broker.stats.net_pnl + self.unrealized()

    def _push(self, buf: list, item: dict) -> None:
        buf.append(item)
        if len(buf) > self.cfg.max_history:
            del buf[: len(buf) - self.cfg.max_history]

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        s = self.broker.stats
        open_pos = []
        for (mid, token), pos in self.broker.positions.items():
            if pos.settled or pos.shares <= 1e-9:
                continue
            m = self._by_id.get(mid)
            mark = (m.market_yes_fair if pos.side == Side.BUY_YES
                    else 1 - m.market_yes_fair) if m else 0.0
            open_pos.append({
                "market_id": mid, "side": pos.side.value,
                "shares": round(pos.shares, 2), "avg_price": round(pos.avg_price(), 4),
                "cost": round(pos.cost_basis, 2), "mark": round(mark, 4),
                "unrealized": round(pos.shares * mark - pos.cost_basis, 2),
                "hours_to_settle": round((m.settle_dt - self.u.now).total_seconds() / 3600, 1)
                if m else None,
                "question": pos.question,
            })
        return {
            "now": self.u.now.isoformat(),
            "tick": self.tick_count,
            "account": {
                "starting_cash": round(self.cfg.starting_cash, 2),
                "equity": round(self.equity(), 2),
                "net_pnl": round(s.net_pnl, 2),
                "gross_pnl": round(s.gross_pnl, 2),
                "fees": round(s.fees, 4),
                "unrealized": round(self.unrealized(), 2),
            },
            "execution": s.as_dict(),
            "counts": {
                "markets": len(self.u.markets),
                "active": len(self.u.active()),
                "open_positions": len(open_pos),
                "resolved": len(self._settled),
            },
            "open_positions": sorted(open_pos, key=lambda x: -abs(x["unrealized"])),
            "recent_signals": list(reversed(self.signals[-15:])),
            "recent_fills": list(reversed(self.fills[-15:])),
            "recent_settlements": list(reversed(self.settlements[-15:])),
            "equity_curve": self.equity_curve[-100:],
        }
