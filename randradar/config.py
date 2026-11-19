"""Load and validate config.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .routes import Market, Route, Step


@dataclass
class Config:
    interval_s: float
    stale_after_s: float
    max_book_age_s: float
    notional_zar: float
    depth_pct: float
    db_path: str
    markets: dict[str, Market]
    routes: list[Route]
    taker_fee: dict[str, float]               # fraction
    withdrawal_fee: dict[str, dict[str, float]]
    fx_refresh_s: float
    usdt_zar_market: str | None


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())

    markets: dict[str, Market] = {}
    for mid, m in raw["markets"].items():
        ex, symbol = mid.split(":", 1)
        markets[mid] = Market(mid, ex, symbol, m["base"].upper(), m["quote"].upper())

    routes: list[Route] = []
    for name, steps in raw["routes"].items():
        parsed = []
        for s in steps:
            side, mid = s.split(maxsplit=1)
            if side not in ("buy", "sell"):
                raise ValueError(f"route {name}: step '{s}' must start with buy/sell")
            if mid not in markets:
                raise ValueError(f"route {name}: unknown market {mid}")
            parsed.append(Step(mid, side))
        _check_route_closes(name, parsed, markets)
        routes.append(Route(name, tuple(parsed)))

    fees = raw["fees"]
    return Config(
        interval_s=float(raw.get("interval_s", 3)),
        stale_after_s=float(raw.get("stale_after_s", 30)),
        max_book_age_s=float(raw.get("max_book_age_s", 8)),
        notional_zar=float(raw.get("notional_zar", 20000)),
        depth_pct=float(raw.get("depth_pct", 1.0)),
        db_path=str(raw.get("db_path", "data/arb.sqlite")),
        markets=markets,
        routes=routes,
        taker_fee={ex: float(v) / 100 for ex, v in fees["taker_pct"].items()},
        withdrawal_fee={ex: {a.upper(): float(x) for a, x in d.items()}
                        for ex, d in fees.get("withdrawal", {}).items()},
        fx_refresh_s=float(raw.get("fx_refresh_s", 3600)),
        usdt_zar_market=raw.get("usdt_zar_market"),
    )


def _check_route_closes(name: str, steps: list[Step], markets: dict[str, Market]) -> None:
    """Each step must spend what the previous step produced, starting and ending in ZAR."""
    holding = "ZAR"
    for s in steps:
        m = markets[s.market_id]
        spend = m.quote if s.side == "buy" else m.base
        if spend != holding:
            raise ValueError(f"route {name}: step '{s.side} {s.market_id}' spends {spend} but we hold {holding}")
        holding = m.base if s.side == "buy" else m.quote
    if holding != "ZAR":
        raise ValueError(f"route {name}: ends holding {holding}, must end in ZAR")
