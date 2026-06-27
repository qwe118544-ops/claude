"""Live Polymarket client: list weather markets and read their prices/book.

Data sources (public, read-only, no auth):
  * Gamma API  (gamma-api.polymarket.com) — market & event metadata, the
    question text, and the CLOB token ids for each outcome.
  * CLOB API   (clob.polymarket.com) — current price and order book for a token.

IMPORTANT — UNVERIFIED AGAINST LIVE HERE: this environment's egress policy
blocks these hosts, so the exact JSON shapes below could not be confirmed in
this session. They follow Polymarket's documented APIs; treat the field
mappings as the thing to check on the first live run, and keep parsing
defensive. The scanner accepts any object implementing `MarketClient`, so the
decision logic is tested offline with a fake client regardless.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .markets import WeatherMarket, parse_weather_question


class PolymarketClient:
    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"

    def __init__(
        self,
        city_coords: Optional[dict] = None,
        weather_query: str = "temperature",
        timeout: int = 60,
        session=None,
    ) -> None:
        # city_coords: {"new york": (40.78, -73.97, "America/New_York"), ...}
        self.city_coords = {k.lower(): v for k, v in (city_coords or {}).items()}
        self.weather_query = weather_query
        self.timeout = timeout
        self._session = session

    def _get(self, url: str, params: dict | None = None) -> object:
        import requests

        sess = self._session or requests
        resp = sess.get(url, params=params or {}, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"GET {url} failed: HTTP {resp.status_code} - {resp.text[:200]}")
        return resp.json()

    # ----- discovery ------------------------------------------------------- #
    def list_weather_markets(self, limit: int = 200) -> list[WeatherMarket]:
        raw = self._get(
            f"{self.GAMMA_URL}/markets",
            {"active": "true", "closed": "false", "limit": limit, "q": self.weather_query},
        )
        items = raw if isinstance(raw, list) else raw.get("data", raw.get("markets", []))
        markets: list[WeatherMarket] = []
        for it in items:
            m = self._build_market(it)
            if m is not None:
                markets.append(m)
        return markets

    def _build_market(self, it: dict) -> Optional[WeatherMarket]:
        question = it.get("question") or it.get("title") or ""
        if "temp" not in question.lower():
            return None
        # YES token id: outcomes/tokens vary by payload; try common shapes.
        yes_token = self._extract_yes_token(it)
        if yes_token is None:
            return None

        parsed = parse_weather_question(question)
        m = WeatherMarket(
            market_id=str(it.get("id") or it.get("conditionId") or yes_token),
            question=question,
            yes_token_id=yes_token,
            threshold_f=parsed["threshold_f"],
            direction=parsed["direction"],
            settle_date=parsed["settle_date"],
            parse_confident=parsed["confident"],
            notes=list(parsed["notes"]),
        )
        # Geolocate from the supplied city table.
        for city, coords in self.city_coords.items():
            if city in question.lower():
                m.city = city
                m.latitude, m.longitude = coords[0], coords[1]
                m.timezone_name = coords[2] if len(coords) > 2 else "auto"
                break
        if m.city is None:
            m.notes.append("city not in supplied coords table; skipped for trading")
            m.parse_confident = False
        return m

    @staticmethod
    def _extract_yes_token(it: dict) -> Optional[str]:
        # clobTokenIds is often a JSON-encoded ["yesId","noId"].
        toks = it.get("clobTokenIds") or it.get("tokens")
        if isinstance(toks, str):
            import json
            try:
                toks = json.loads(toks)
            except Exception:
                return None
        if isinstance(toks, list) and toks:
            first = toks[0]
            return first.get("token_id") if isinstance(first, dict) else str(first)
        return None

    # ----- live state ------------------------------------------------------ #
    def fill_book(self, market: WeatherMarket) -> WeatherMarket:
        """Populate yes_price / best_bid / best_ask / top sizes from the CLOB."""
        book = self._get(f"{self.CLOB_URL}/book", {"token_id": market.yes_token_id})
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if bids:
            best = max(bids, key=lambda b: float(b["price"]))
            market.best_bid = float(best["price"])
            market.top_bid_size = float(best.get("size", 0))
        if asks:
            best = min(asks, key=lambda a: float(a["price"]))
            market.best_ask = float(best["price"])
            market.top_ask_size = float(best.get("size", 0))
        if market.best_bid is not None and market.best_ask is not None:
            market.yes_price = (market.best_bid + market.best_ask) / 2.0
        elif market.best_bid is not None:
            market.yes_price = market.best_bid
        elif market.best_ask is not None:
            market.yes_price = market.best_ask
        return market
