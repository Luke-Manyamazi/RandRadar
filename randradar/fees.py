"""Fetch YOUR account's real fees from VALR, Luno and Binance.

Needs READ-ONLY API keys in a .env file (see .env.example). The keys are only
used to read fee information. Nothing here can trade or withdraw.

Anything that cannot be fetched (no key, endpoint changed, value looks wrong)
falls back to the figure in config.yaml, and the report says so.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .config import Config

log = logging.getLogger("randradar.fees")

VALR = "https://api.valr.com"
LUNO = "https://api.luno.com"
BINANCE = "https://api.binance.com"

# Sanity bounds: a taker fee outside -0.1%..1% means we parsed something wrong.
MIN_TAKER, MAX_TAKER = -0.001, 0.01


@dataclass
class FeeResult:
    taker: dict[str, float] = field(default_factory=dict)               # market_id -> fraction
    withdrawal: dict[str, dict[str, float]] = field(default_factory=dict)  # exchange -> asset -> amount
    notes: list[str] = field(default_factory=list)                      # human-readable report lines

    def ok(self, msg: str) -> None:
        self.notes.append(f"OK    {msg}")

    def skip(self, msg: str) -> None:
        self.notes.append(f"SKIP  {msg}")

    def fail(self, msg: str) -> None:
        self.notes.append(f"FAIL  {msg}")


# --------------------------------------------------------------------------- .env

def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    env: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(env) + [k for k in os.environ if k.split("_")[0] in ("VALR", "LUNO", "BINANCE")]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# --------------------------------------------------------------------------- signing

def valr_signature(secret: str, timestamp_ms: str, verb: str, path: str, body: str = "") -> str:
    """VALR: hex HMAC-SHA512 of timestamp + VERB + path(with query) + body."""
    msg = f"{timestamp_ms}{verb.upper()}{path}{body}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha512).hexdigest()


def binance_signature(secret: str, query: str) -> str:
    """Binance: hex HMAC-SHA256 of the full query string."""
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _redact(text: str, env: dict[str, str]) -> str:
    for v in env.values():
        if v and len(v) > 6:
            text = text.replace(v, "***")
    return text


def _in_bounds(x: float) -> bool:
    return MIN_TAKER <= x <= MAX_TAKER


def _num(d: dict, *keys: str) -> Optional[float]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return float(d[k])
    return None


# --------------------------------------------------------------------------- VALR

async def _valr_get(http: httpx.AsyncClient, env: dict, path: str) -> Any:
    ts = str(int(time.time() * 1000))
    headers = {
        "X-VALR-API-KEY": env["VALR_API_KEY"],
        "X-VALR-SIGNATURE": valr_signature(env["VALR_API_SECRET"], ts, "GET", path),
        "X-VALR-TIMESTAMP": ts,
    }
    r = await http.get(VALR + path, headers=headers)
    r.raise_for_status()
    return r.json()


def parse_valr_trade_fees(payload: Any) -> dict[str, float]:
    """Accepts a list of {currencyPair, ...taker...} rows. Fields named *Percentage are in %."""
    rows = payload if isinstance(payload, list) else payload.get("fees", payload.get("data", []))
    out: dict[str, float] = {}
    for row in rows or []:
        pair = row.get("currencyPair") or row.get("pair") or row.get("symbol")
        if not pair:
            continue
        pct = _num(row, "takerPercentage", "takerFeePercentage")
        frac = _num(row, "takerFee", "taker", "takerFeeRate")
        if pct is not None:
            out[pair.upper()] = pct / 100
        elif frac is not None:
            out[pair.upper()] = frac
    return out


async def fetch_valr(cfg: Config, http: httpx.AsyncClient, env: dict, res: FeeResult) -> None:
    if not env.get("VALR_API_KEY") or not env.get("VALR_API_SECRET"):
        res.skip("valr: no VALR_API_KEY / VALR_API_SECRET in .env, using config.yaml")
        return
    markets = [m for m in cfg.markets.values() if m.exchange == "valr"]
    try:
        fees = parse_valr_trade_fees(await _valr_get(http, env, "/v1/account/fees/trade"))
        for m in markets:
            f = fees.get(m.symbol.upper())
            if f is None:
                res.fail(f"valr {m.symbol} trade fee: pair not in response, using config.yaml")
            elif not _in_bounds(f):
                res.fail(f"valr {m.symbol} trade fee {f:.5f} looks wrong, using config.yaml")
            else:
                res.taker[m.market_id] = f
                res.ok(f"valr {m.symbol} taker {f * 100:.3f}%")
    except Exception as exc:  # noqa: BLE001
        res.fail(f"valr trade fees: {_redact(str(exc), env)[:160]}, using config.yaml")

    for asset in sorted({a for m in markets for a in (m.base,) if a != "ZAR"}):
        try:
            info = await _valr_get(http, env, f"/v1/wallet/crypto/{asset}/withdraw")
            cost = _num(info, "withdrawCost", "withdrawalCost", "withdrawFee")
            if cost is None or cost < 0:
                raise ValueError(f"no withdrawCost in response keys {list(info)[:6]}")
            res.withdrawal.setdefault("valr", {})[asset] = cost
            res.ok(f"valr {asset} withdrawal fee {cost:g} {asset}")
        except Exception as exc:  # noqa: BLE001
            res.fail(f"valr {asset} withdrawal fee: {_redact(str(exc), env)[:160]}, using config.yaml")


# --------------------------------------------------------------------------- Luno

async def fetch_luno(cfg: Config, http: httpx.AsyncClient, env: dict, res: FeeResult) -> None:
    if not env.get("LUNO_API_KEY_ID") or not env.get("LUNO_API_SECRET"):
        res.skip("luno: no LUNO_API_KEY_ID / LUNO_API_SECRET in .env, using config.yaml")
        return
    auth = (env["LUNO_API_KEY_ID"], env["LUNO_API_SECRET"])
    for m in (m for m in cfg.markets.values() if m.exchange == "luno"):
        try:
            r = await http.get(f"{LUNO}/api/1/fee_info", params={"pair": m.symbol}, auth=auth)
            r.raise_for_status()
            f = _num(r.json(), "taker_fee")
            if f is None or not _in_bounds(f):
                raise ValueError(f"unexpected taker_fee {f!r}")
            res.taker[m.market_id] = f
            res.ok(f"luno {m.symbol} taker {f * 100:.3f}%")
        except Exception as exc:  # noqa: BLE001
            res.fail(f"luno {m.symbol} trade fee: {_redact(str(exc), env)[:160]}, using config.yaml")
    res.skip("luno withdrawal fees: Luno only quotes these per address, using config.yaml")


# --------------------------------------------------------------------------- Binance

async def _binance_get(http: httpx.AsyncClient, env: dict, path: str, params: dict, offset_ms: int) -> Any:
    q = dict(params, recvWindow=10000, timestamp=int(time.time() * 1000) + offset_ms)
    query = urlencode(q)
    query += "&signature=" + binance_signature(env["BINANCE_API_SECRET"], query)
    r = await http.get(f"{BINANCE}{path}?{query}", headers={"X-MBX-APIKEY": env["BINANCE_API_KEY"]})
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}: {r.text[:160]}", request=r.request, response=r)
    return r.json()


def pick_binance_withdraw_fee(coins: list[dict], coin: str, network: Optional[str]) -> Optional[tuple[str, float]]:
    for c in coins:
        if c.get("coin") != coin:
            continue
        nets = [n for n in c.get("networkList", []) if str(n.get("withdrawEnable", True)).lower() != "false"]
        chosen = next((n for n in nets if n.get("network") == network), None) if network else None
        chosen = chosen or next((n for n in nets if n.get("isDefault")), None) or (nets[0] if nets else None)
        if chosen:
            return chosen["network"], float(chosen["withdrawFee"])
    return None


async def fetch_binance(cfg: Config, http: httpx.AsyncClient, env: dict, res: FeeResult,
                        networks: dict[str, str]) -> None:
    if not env.get("BINANCE_API_KEY") or not env.get("BINANCE_API_SECRET"):
        res.skip("binance: no BINANCE_API_KEY / BINANCE_API_SECRET in .env, using config.yaml")
        return
    offset = 0
    try:  # PC clocks drift; Binance rejects requests >10s off, so align to server time
        r = await http.get(f"{BINANCE}/api/v3/time")
        offset = int(r.json()["serverTime"]) - int(time.time() * 1000)
    except Exception:  # noqa: BLE001
        pass

    markets = [m for m in cfg.markets.values() if m.exchange == "binance"]
    for m in markets:
        try:
            rows = await _binance_get(http, env, "/sapi/v1/asset/tradeFee", {"symbol": m.symbol}, offset)
            row = rows[0] if isinstance(rows, list) and rows else rows
            f = _num(row, "takerCommission")
            if f is None or not _in_bounds(f):
                raise ValueError(f"unexpected takerCommission {f!r}")
            res.taker[m.market_id] = f
            res.ok(f"binance {m.symbol} taker {f * 100:.3f}% (BNB discount not included)")
        except Exception as exc:  # noqa: BLE001
            res.fail(f"binance {m.symbol} trade fee: {_redact(str(exc), env)[:160]}, using config.yaml")

    try:
        coins = await _binance_get(http, env, "/sapi/v1/capital/config/getall", {}, offset)
        for asset in sorted({a for m in markets for a in (m.base, m.quote)}):
            picked = pick_binance_withdraw_fee(coins, asset, networks.get(asset))
            if picked:
                net, fee = picked
                res.withdrawal.setdefault("binance", {})[asset] = fee
                res.ok(f"binance {asset} withdrawal fee {fee:g} {asset} via {net}")
            else:
                res.fail(f"binance {asset} withdrawal fee: coin/network not found, using config.yaml")
    except Exception as exc:  # noqa: BLE001
        res.fail(f"binance withdrawal fees: {_redact(str(exc), env)[:160]}, using config.yaml")


# --------------------------------------------------------------------------- entry point

async def fetch_all(cfg: Config, http: httpx.AsyncClient, env: Optional[dict] = None) -> FeeResult:
    env = env if env is not None else load_env()
    res = FeeResult()
    await fetch_valr(cfg, http, env, res)
    await fetch_luno(cfg, http, env, res)
    await fetch_binance(cfg, http, env, res, cfg.withdraw_networks.get("binance", {}))
    return res


def merged(cfg: Config, res: FeeResult) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Config defaults overlaid with whatever was fetched successfully."""
    taker = dict(cfg.taker_fee)
    taker.update(res.taker)
    wd = {ex: dict(d) for ex, d in cfg.withdrawal_fee.items()}
    for ex, d in res.withdrawal.items():
        wd.setdefault(ex, {}).update(d)
    return taker, wd
