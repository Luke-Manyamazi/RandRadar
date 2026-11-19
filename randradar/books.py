"""Order book model and per-exchange response parsers.

Each exchange returns its order book in a different JSON shape. The parsers
below turn them all into one OrderBook so the spread maths never needs to know
which exchange it is looking at.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

Level = tuple[float, float]  # (price, quantity in base asset)


class BookError(ValueError):
    """Raised when an exchange response cannot be turned into a sane order book."""


@dataclass(frozen=True)
class OrderBook:
    market_id: str            # e.g. "valr:BTCZAR"
    bids: list[Level]         # highest price first
    asks: list[Level]         # lowest price first
    ts: float                 # unix time we received it
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    def depth_within(self, pct: float, side: str) -> float:
        """Value (in quote currency) resting within `pct`% of the best price.

        side="ask": liquidity you could BUY from; side="bid": liquidity you could SELL into.
        """
        if side == "ask":
            limit = self.best_ask * (1 + pct / 100)
            return sum(p * q for p, q in self.asks if p <= limit)
        if side == "bid":
            limit = self.best_bid * (1 - pct / 100)
            return sum(p * q for p, q in self.bids if p >= limit)
        raise ValueError(side)


def _levels(raw: Iterable[Any], price_key: str | int, qty_keys: tuple) -> list[Level]:
    out: list[Level] = []
    for entry in raw or []:
        try:
            price = float(entry[price_key])
            qty = None
            for k in qty_keys:
                try:
                    qty = float(entry[k])
                    break
                except (KeyError, IndexError, TypeError):
                    continue
            if qty is None:
                raise KeyError("quantity")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BookError(f"bad level {entry!r}: {exc}") from exc
        if price > 0 and qty > 0:
            out.append((price, qty))
    return out


def _finish(market_id: str, bids: list[Level], asks: list[Level], ts: float) -> OrderBook:
    bids = sorted(bids, key=lambda l: -l[0])
    asks = sorted(asks, key=lambda l: l[0])
    if not bids or not asks:
        raise BookError(f"{market_id}: empty side (bids={len(bids)}, asks={len(asks)})")
    if bids[0][0] >= asks[0][0]:
        raise BookError(f"{market_id}: crossed book bid {bids[0][0]} >= ask {asks[0][0]}")
    return OrderBook(market_id, bids, asks, ts)


def parse_valr(market_id: str, payload: dict, ts: float) -> OrderBook:
    # VALR: {"Asks":[{"side":"sell","quantity":"0.1","price":"1000000","currencyPair":"BTCZAR",...}], "Bids":[...]}
    asks_raw = payload.get("Asks", payload.get("asks"))
    bids_raw = payload.get("Bids", payload.get("bids"))
    if asks_raw is None or bids_raw is None:
        raise BookError(f"{market_id}: unexpected VALR payload keys {list(payload)[:6]}")
    return _finish(market_id, _levels(bids_raw, "price", ("quantity", "volume")),
                   _levels(asks_raw, "price", ("quantity", "volume")), ts)


def parse_luno(market_id: str, payload: dict, ts: float) -> OrderBook:
    # Luno: {"timestamp":..., "bids":[{"price":"...","volume":"..."}], "asks":[...]}
    if "asks" not in payload or "bids" not in payload:
        raise BookError(f"{market_id}: unexpected Luno payload keys {list(payload)[:6]}")
    return _finish(market_id, _levels(payload["bids"], "price", ("volume", "quantity")),
                   _levels(payload["asks"], "price", ("volume", "quantity")), ts)


def parse_binance(market_id: str, payload: dict, ts: float) -> OrderBook:
    # Binance: {"lastUpdateId":..., "bids":[["price","qty"],...], "asks":[...]}
    if "asks" not in payload or "bids" not in payload:
        raise BookError(f"{market_id}: unexpected Binance payload keys {list(payload)[:6]}")
    return _finish(market_id, _levels(payload["bids"], 0, (1,)),
                   _levels(payload["asks"], 0, (1,)), ts)


PARSERS = {"valr": parse_valr, "luno": parse_luno, "binance": parse_binance}
