"""HTTP clients for public order books, with backoff and a per-market circuit breaker.

All endpoints here are PUBLIC: no API keys are needed or used by the logger.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .books import PARSERS, BookError, OrderBook
from .routes import Market

log = logging.getLogger("randradar.exchanges")

ENDPOINTS = {
    # VALR public order book (aggregated). Docs: https://docs.valr.com  (Public API -> Order Book)
    "valr": "https://api.valr.com/v1/public/{symbol}/orderbook",
    # Luno top-100 bids/asks. Docs: https://www.luno.com/en/developers/api  (GET /api/1/orderbook_top)
    "luno": "https://api.luno.com/api/1/orderbook_top?pair={symbol}",
    # Binance spot depth, limit 100 = request weight 5.
    "binance": "https://api.binance.com/api/v3/depth?symbol={symbol}&limit=100",
}

FX_ENDPOINTS = [
    # Frankfurter serves European Central Bank reference rates, free, no key.
    ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=ZAR", ("rates", "ZAR")),
    ("https://api.frankfurter.app/latest?from=USD&to=ZAR", ("rates", "ZAR")),
    ("https://open.er-api.com/v6/latest/USD", ("rates", "ZAR")),
]

MAX_BACKOFF_S = 60.0
BREAKER_FAILS = 3          # consecutive failures before the market is paused
INVALID_RETRY_S = 600.0    # a 400/404 (bad symbol) is retried every 10 min, not every tick


@dataclass
class MarketHealth:
    consecutive_failures: int = 0
    paused_until: float = 0.0
    last_ok: float = 0.0
    last_error: str = ""
    total_ok: int = 0
    total_fail: int = 0
    invalid: bool = False

    def record_ok(self, now: float) -> None:
        self.consecutive_failures = 0
        self.paused_until = 0.0
        self.last_ok = now
        self.total_ok += 1
        self.invalid = False

    def record_fail(self, now: float, err: str, invalid: bool = False, retry_after: float | None = None) -> None:
        self.consecutive_failures += 1
        self.total_fail += 1
        self.last_error = err
        self.invalid = invalid
        if invalid:
            self.paused_until = now + INVALID_RETRY_S
        elif retry_after:
            self.paused_until = now + min(retry_after, MAX_BACKOFF_S * 5)
        elif self.consecutive_failures >= BREAKER_FAILS:
            # 1s, 2s, 4s ... capped at 60s
            backoff = min(2 ** (self.consecutive_failures - BREAKER_FAILS), MAX_BACKOFF_S)
            self.paused_until = now + backoff


@dataclass
class BookFetcher:
    http: httpx.AsyncClient
    health: dict[str, MarketHealth] = field(default_factory=dict)

    async def fetch(self, market: Market) -> Optional[OrderBook]:
        h = self.health.setdefault(market.market_id, MarketHealth())
        now = time.time()
        if now < h.paused_until:
            return None
        url = ENDPOINTS[market.exchange].format(symbol=market.symbol)
        try:
            r = await self.http.get(url)
            if r.status_code == 429 or r.status_code == 418:
                retry = float(r.headers.get("Retry-After", 30))
                h.record_fail(now, f"rate limited HTTP {r.status_code}", retry_after=retry)
                log.warning("%s rate limited, pausing %.0fs", market.market_id, retry)
                return None
            if r.status_code in (400, 404):
                h.record_fail(now, f"HTTP {r.status_code}: {r.text[:200]}", invalid=True)
                log.error("%s rejected (HTTP %s) - check symbol/endpoint: %s",
                          market.market_id, r.status_code, r.text[:200])
                return None
            r.raise_for_status()
            book = PARSERS[market.exchange](market.market_id, r.json(), time.time())
            book.meta["latency_ms"] = (time.time() - now) * 1000
            h.record_ok(time.time())
            return book
        except (httpx.HTTPError, BookError, ValueError) as exc:
            h.record_fail(now, f"{type(exc).__name__}: {exc}"[:300])
            log.warning("%s fetch failed (%d in a row): %s", market.market_id, h.consecutive_failures, exc)
            return None


async def fetch_usdzar(http: httpx.AsyncClient) -> Optional[float]:
    for url, path in FX_ENDPOINTS:
        try:
            r = await http.get(url)
            r.raise_for_status()
            v = r.json()
            for k in path:
                v = v[k]
            rate = float(v)
            if 5 < rate < 50:   # sanity bound for USD/ZAR
                return rate
        except Exception as exc:  # noqa: BLE001 - try the next source
            log.warning("FX source %s failed: %s", url, exc)
    return None
