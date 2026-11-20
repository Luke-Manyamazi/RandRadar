"""Main polling loop. Never crashes on a single bad tick; logs the problem and carries on."""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Awaitable, Callable, Optional

import httpx

from .books import OrderBook
from .config import Config
from .exchanges import BookFetcher, fetch_usdzar
from .fees import FeeResult, fetch_all, merged
from .routes import Market, evaluate
from .store import Store

log = logging.getLogger("randradar")

FetchFn = Callable[[Market], Awaitable[Optional[OrderBook]]]
FxFn = Callable[[], Awaitable[Optional[float]]]
FeeFn = Callable[[], Awaitable[Optional["FeeResult"]]]


class Logger:
    def __init__(self, cfg: Config, store: Store, fetch: FetchFn, fx: FxFn, fees: Optional[FeeFn] = None):
        self.cfg, self.store, self.fetch, self.fx, self.fee_fn = cfg, store, fetch, fx, fees
        self.taker_fee = dict(cfg.taker_fee)
        self.withdrawal_fee = {ex: dict(d) for ex, d in cfg.withdrawal_fee.items()}
        self.fees_at = 0.0
        self.books: dict[str, OrderBook] = {}
        self.usdzar: Optional[float] = None
        self.fx_at = 0.0
        self.stop = asyncio.Event()
        self.ticks = 0
        self.rows = 0
        self.started = time.time()
        self.last_beat = 0.0
        self.down_since: dict[str, float] = {}

    async def refresh_fx(self) -> None:
        if time.time() - self.fx_at < self.cfg.fx_refresh_s and self.usdzar:
            return
        rate = await self.fx()
        self.fx_at = time.time()  # even on failure: don't hammer FX sources every tick
        if rate:
            self.usdzar = rate
        else:
            self.store.event("WARN", "fx", "all USD/ZAR sources failed; keeping last value")

    async def refresh_fees(self) -> None:
        """Read the account's real fees (read-only keys) once a day; config.yaml is the fallback."""
        if self.fee_fn is None or time.time() - self.fees_at < self.cfg.fee_refresh_s:
            return
        self.fees_at = time.time()
        try:
            res = await self.fee_fn()
        except Exception as exc:  # noqa: BLE001
            self.store.event("WARN", "fees", f"fee refresh failed, keeping current fees: {exc!r}"[:500])
            return
        if res is None:
            return
        taker, wd = merged(self.cfg, res)
        if taker != self.taker_fee or wd != self.withdrawal_fee:
            self.store.event("INFO", "fees", "fees updated: " + json.dumps({"taker": taker, "withdrawal": wd}))
            log.info("fees updated from accounts (%d fetched values)", len(res.taker) + sum(map(len, res.withdrawal.values())))
        self.taker_fee, self.withdrawal_fee = taker, wd
        for line in res.notes:
            if not line.startswith("OK"):
                log.warning("fees: %s", line)
        self._snapshot_fees()

    def _snapshot_fees(self) -> None:
        self.store.fee_snapshot(json.dumps({"taker_fee": self.taker_fee, "withdrawal_fee": self.withdrawal_fee,
                                            "notional_zar": self.cfg.notional_zar}))

    async def tick(self) -> None:
        now = time.time()
        await self.refresh_fees()
        results = await asyncio.gather(*(self.fetch(m) for m in self.cfg.markets.values()))
        quote_rows = []
        for m, book in zip(self.cfg.markets.values(), results):
            if book is not None:
                self.books[m.market_id] = book
                quote_rows.append((book.ts, m.market_id, book.best_bid, book.best_ask,
                                   book.depth_within(self.cfg.depth_pct, "bid"),
                                   book.depth_within(self.cfg.depth_pct, "ask"),
                                   book.meta.get("latency_ms")))

        alive = {mid for mid, b in self.books.items() if now - b.ts <= self.cfg.stale_after_s}
        self._track_down(alive, now)
        # Routes only use books young enough that all legs describe the same moment.
        fresh = {mid: b for mid, b in self.books.items() if now - b.ts <= self.cfg.max_book_age_s}
        await self.refresh_fx()

        premium = None
        u = self.cfg.usdt_zar_market
        if self.usdzar and u and u in fresh:
            premium = (fresh[u].mid / self.usdzar - 1) * 100

        spread_rows = []
        for route in self.cfg.routes:
            if not all(s.market_id in fresh for s in route.steps):
                continue  # a leg is stale/down: never compute on old prices
            r = evaluate(route, self.cfg.markets, fresh, self.taker_fee,
                         self.withdrawal_fee, self.cfg.notional_zar)
            spread_rows.append((now, r.route, r.gross_pct, r.net_inventory_pct, r.net_transfer_pct,
                                int(r.fillable), self.cfg.notional_zar, self.usdzar, premium))

        self.store.add_quotes(quote_rows)
        self.store.add_spreads(spread_rows)
        self.store.commit()
        self.ticks += 1
        self.rows += len(spread_rows)

    def _track_down(self, alive: set, now: float) -> None:
        for mid in self.cfg.markets:
            if mid not in alive:
                if mid not in self.down_since:
                    self.down_since[mid] = now
                    self.store.event("WARN", mid, "market stale/down")
                    log.warning("%s marked DOWN (no fresh book)", mid)
            elif mid in self.down_since:  # noqa: SIM114
                dur = now - self.down_since.pop(mid)
                self.store.event("INFO", mid, f"market recovered after {dur:.0f}s")
                log.info("%s recovered after %.0fs", mid, dur)

    def heartbeat(self) -> None:
        if time.time() - self.last_beat < 60:
            return
        self.last_beat = time.time()
        up = (time.time() - self.started) / 3600
        down = ", ".join(self.down_since) or "none"
        log.info("alive %.1fh | ticks=%d spread_rows=%d | USD/ZAR=%s | down: %s",
                 up, self.ticks, self.rows, self.usdzar and round(self.usdzar, 4), down)

    async def run(self, max_ticks: Optional[int] = None) -> None:
        self._snapshot_fees()
        self.store.event("INFO", "logger", "started")
        self.store.commit()
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill a 4-week study
                log.exception("tick failed")
                self.store.event("ERROR", "tick", repr(exc))
                self.store.commit()
            self.heartbeat()
            if max_ticks and self.ticks >= max_ticks:
                break
            delay = max(0.0, self.cfg.interval_s - (time.monotonic() - t0))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self.store.event("INFO", "logger", "stopped")
        self.store.close()


async def main(cfg: Config) -> None:
    headers = {"User-Agent": "randradar/0.1-beta (research; public data only)"}
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as http:
        fetcher = BookFetcher(http)
        lg = Logger(cfg, Store(cfg.db_path), fetcher.fetch, lambda: fetch_usdzar(http),
                    lambda: fetch_all(cfg, http))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lg.stop.set)
            except NotImplementedError:  # Windows
                pass
        log.info("logging %d markets, %d routes every %.0fs -> %s",
                 len(cfg.markets), len(cfg.routes), cfg.interval_s, cfg.db_path)
        await lg.run()


async def check(cfg: Config) -> bool:
    """One-shot connectivity check: fetch every market once and print what came back."""
    ok = True
    async with httpx.AsyncClient(timeout=10.0) as http:
        fetcher = BookFetcher(http)
        for m in cfg.markets.values():
            b = await fetcher.fetch(m)
            if b:
                print(f"OK    {m.market_id:18} bid {b.best_bid:>14,.4f}  ask {b.best_ask:>14,.4f}  "
                      f"levels {len(b.bids)}/{len(b.asks)}  {b.meta['latency_ms']:.0f}ms")
            else:
                ok = False
                print(f"FAIL  {m.market_id:18} {fetcher.health[m.market_id].last_error}")
        rate = await fetch_usdzar(http)
        print(f"{'OK' if rate else 'FAIL':5} USD/ZAR reference  {rate}")
        ok = ok and bool(rate)
    return ok


async def check_fees(cfg: Config) -> bool:
    """Show which fees come from your accounts and which fall back to config.yaml."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        res = await fetch_all(cfg, http)
    for line in res.notes:
        print(line)
    taker, wd = merged(cfg, res)
    print("\nFees the logger will use:")
    for m in cfg.markets.values():
        f = taker.get(m.market_id, taker.get(m.exchange, 0.0))
        src = "account" if m.market_id in res.taker else "config.yaml"
        print(f"  {m.market_id:18} taker {f * 100:.3f}%   ({src})")
    for ex, d in sorted(wd.items()):
        for asset, v in sorted(d.items()):
            src = "account" if asset in res.withdrawal.get(ex, {}) else "config.yaml"
            print(f"  {ex:8} withdraw {asset:5} {v:g}   ({src})")
    return not any(n.startswith("FAIL") for n in res.notes)
