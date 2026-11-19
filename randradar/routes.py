"""Arbitrage route simulation.

A route is a chain of trades that starts and ends in ZAR, e.g.
    buy BTC with ZAR on VALR  ->  sell BTC for ZAR on Luno
or the offshore loop
    buy USDT with ZAR on VALR -> buy BTC with USDT on Binance -> sell BTC for ZAR on VALR

Instead of a top-of-book formula with a guessed slippage %, we walk the real
order book for the configured trade size. Slippage is therefore *measured*,
not assumed.

Two net figures are produced for every route:
  * inventory  - you already hold funds on every exchange (how real arb desks work);
                 only trading fees + book depth count.
  * transfer   - you must physically move the asset between exchanges each time;
                 withdrawal / network / bank fees are subtracted as well.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Optional

from .books import Level, OrderBook


@dataclass(frozen=True)
class Market:
    market_id: str   # "valr:BTCZAR"
    exchange: str    # "valr"
    symbol: str      # "BTCZAR" (as the exchange spells it)
    base: str        # "BTC"
    quote: str       # "ZAR"


@dataclass(frozen=True)
class Step:
    market_id: str
    side: Literal["buy", "sell"]   # buy = spend quote, receive base; sell = spend base, receive quote


@dataclass(frozen=True)
class Route:
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class RouteResult:
    route: str
    gross_pct: float                  # top-of-book, no fees, no depth
    net_inventory_pct: Optional[float]  # None = book too thin to fill the notional
    net_transfer_pct: Optional[float]
    fillable: bool


def fill_buy(asks: list[Level], quote_amount: float) -> Optional[float]:
    """Spend `quote_amount` walking up the asks. Returns base received, or None if book too thin."""
    remaining, base = quote_amount, 0.0
    for price, qty in asks:
        cost = price * qty
        if cost >= remaining:
            return base + remaining / price
        base += qty
        remaining -= cost
    return None


def fill_sell(bids: list[Level], base_amount: float) -> Optional[float]:
    """Sell `base_amount` walking down the bids. Returns quote received, or None if book too thin."""
    remaining, quote = base_amount, 0.0
    for price, qty in bids:
        if qty >= remaining:
            return quote + remaining * price
        quote += price * qty
        remaining -= qty
    return None


def _out_asset(m: Market, side: str) -> str:
    return m.base if side == "buy" else m.quote


def simulate(
    route: Route,
    markets: Mapping[str, Market],
    books: Mapping[str, OrderBook],
    taker_fee: Mapping[str, float],          # exchange -> fraction (0.001 = 0.10%)
    withdrawal_fee: Mapping[str, Mapping[str, float]],  # exchange -> asset -> fixed amount
    notional_zar: float,
    include_transfers: bool,
) -> Optional[float]:
    """Run `notional_zar` through the route. Returns net return as a fraction, or None if unfillable."""
    amount = notional_zar
    first_ex = markets[route.steps[0].market_id].exchange
    for i, step in enumerate(route.steps):
        m = markets[step.market_id]
        book = books[step.market_id]
        out = fill_buy(book.asks, amount) if step.side == "buy" else fill_sell(book.bids, amount)
        if out is None:
            return None
        amount = out * (1 - taker_fee.get(m.exchange, 0.0))

        nxt = route.steps[i + 1] if i + 1 < len(route.steps) else None
        if include_transfers:
            asset = _out_asset(m, step.side)
            moving_to = markets[nxt.market_id].exchange if nxt else first_ex
            if moving_to != m.exchange:
                amount -= withdrawal_fee.get(m.exchange, {}).get(asset, 0.0)
                if amount <= 0:
                    return -1.0
    return amount / notional_zar - 1


def gross(route: Route, books: Mapping[str, OrderBook]) -> float:
    """Top-of-book round trip with no fees and no depth: the 'headline' spread people screenshot."""
    amount = 1.0
    for step in route.steps:
        b = books[step.market_id]
        amount = amount / b.best_ask if step.side == "buy" else amount * b.best_bid
    return amount - 1


def evaluate(route, markets, books, taker_fee, withdrawal_fee, notional_zar) -> RouteResult:
    inv = simulate(route, markets, books, taker_fee, withdrawal_fee, notional_zar, False)
    trf = simulate(route, markets, books, taker_fee, withdrawal_fee, notional_zar, True)
    pct = lambda x: None if x is None else x * 100
    return RouteResult(
        route=route.name,
        gross_pct=gross(route, books) * 100,
        net_inventory_pct=pct(inv),
        net_transfer_pct=pct(trf),
        fillable=inv is not None,
    )
