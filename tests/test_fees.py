import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from randradar import config as config_mod
from randradar.fees import (binance_signature, fetch_all, load_env, merged, parse_valr_trade_fees,
                            pick_binance_withdraw_fee, valr_signature)
from randradar.routes import Market, Route, Step, simulate
from tests.test_core import book

ROOT = Path(__file__).resolve().parents[1]
ENV = {"VALR_API_KEY": "vk", "VALR_API_SECRET": "vsecret123", "LUNO_API_KEY_ID": "lk",
       "LUNO_API_SECRET": "lsecret123", "BINANCE_API_KEY": "bk", "BINANCE_API_SECRET": "bsecret123"}


def test_valr_signature_matches_documented_scheme():
    # Docs: hex HMAC-SHA512 over timestamp + VERB + path + body
    expected = hmac.new(b"s", b"1700000000000GET/v1/account/fees/trade", hashlib.sha512).hexdigest()
    assert valr_signature("s", "1700000000000", "get", "/v1/account/fees/trade") == expected


def test_binance_signature():
    expected = hmac.new(b"s", b"symbol=BTCUSDT&timestamp=1", hashlib.sha256).hexdigest()
    assert binance_signature("s", "symbol=BTCUSDT&timestamp=1") == expected


def test_parse_valr_fees_percentage_vs_fraction():
    assert parse_valr_trade_fees([{"currencyPair": "BTCZAR", "takerPercentage": 0.1}]) == {"BTCZAR": 0.001}
    assert parse_valr_trade_fees([{"currencyPair": "usdtzar", "takerFee": "0.0005"}]) == {"USDTZAR": 0.0005}


def test_pick_binance_network_preference_and_default():
    coins = [{"coin": "USDT", "networkList": [
        {"network": "ETH", "withdrawFee": "4", "isDefault": True, "withdrawEnable": True},
        {"network": "TRX", "withdrawFee": "1", "isDefault": False, "withdrawEnable": True},
        {"network": "BSC", "withdrawFee": "0.5", "isDefault": False, "withdrawEnable": False}]}]
    assert pick_binance_withdraw_fee(coins, "USDT", "TRX") == ("TRX", 1.0)
    assert pick_binance_withdraw_fee(coins, "USDT", "BSC") == ("ETH", 4.0)   # disabled -> default
    assert pick_binance_withdraw_fee(coins, "USDT", None) == ("ETH", 4.0)
    assert pick_binance_withdraw_fee(coins, "BTC", None) is None


def _mock(valr_fee_status=200):
    seen = []

    def handler(req: httpx.Request):
        u = urlparse(str(req.url))
        seen.append((req.method, u.path))
        if u.netloc == "api.valr.com":
            assert req.headers["X-VALR-API-KEY"] == "vk" and len(req.headers["X-VALR-SIGNATURE"]) == 128
            if u.path == "/v1/account/fees/trade":
                if valr_fee_status != 200:
                    return httpx.Response(valr_fee_status, text="not found")
                return httpx.Response(200, json=[{"currencyPair": "BTCZAR", "takerPercentage": 0.1},
                                                 {"currencyPair": "USDTZAR", "takerPercentage": 0.05}])
            if u.path.endswith("/withdraw"):
                return httpx.Response(200, json={"currency": "BTC", "withdrawCost": "0.00004",
                                                 "minimumWithdrawAmount": "0.0002", "isActive": True})
        if u.netloc == "api.luno.com":
            assert req.headers["Authorization"].startswith("Basic ")
            pair = parse_qs(u.query)["pair"][0]
            return httpx.Response(200, json={"maker_fee": "0.00", "taker_fee": "0.0008" if pair == "XBTZAR" else "0.0004",
                                             "thirty_day_volume": "0"})
        if u.netloc == "api.binance.com":
            if u.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": 1})
            assert req.headers["X-MBX-APIKEY"] == "bk" and "signature=" in u.query
            if u.path == "/sapi/v1/asset/tradeFee":
                return httpx.Response(200, json=[{"symbol": "BTCUSDT", "makerCommission": "0.001",
                                                  "takerCommission": "0.00095"}])
            if u.path == "/sapi/v1/capital/config/getall":
                return httpx.Response(200, json=[
                    {"coin": "BTC", "networkList": [{"network": "BTC", "withdrawFee": "0.00001", "isDefault": True,
                                                     "withdrawEnable": True}]},
                    {"coin": "USDT", "networkList": [{"network": "TRX", "withdrawFee": "1.5", "isDefault": False,
                                                      "withdrawEnable": True}]}])
        return httpx.Response(404)
    return handler, seen


def _run(env, status=200):
    cfg = config_mod.load(ROOT / "config.yaml")
    handler, seen = _mock(status)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fetch_all(cfg, http, env)
    return cfg, asyncio.run(go()), seen


def test_fetch_all_reads_every_exchange_and_only_uses_get():
    cfg, res, seen = _run(ENV)
    assert res.taker["valr:BTCZAR"] == pytest.approx(0.001)
    assert res.taker["valr:USDTZAR"] == pytest.approx(0.0005)
    assert res.taker["luno:XBTZAR"] == pytest.approx(0.0008)
    assert res.taker["binance:BTCUSDT"] == pytest.approx(0.00095)
    assert res.withdrawal["binance"] == {"BTC": 0.00001, "USDT": 1.5}
    assert res.withdrawal["valr"]["BTC"] == pytest.approx(0.00004)
    assert all(method == "GET" for method, _ in seen)        # read-only: never POST/DELETE
    assert not any(n.startswith("FAIL") for n in res.notes)


def test_no_keys_means_config_fallback():
    cfg, res, seen = _run({})
    assert res.taker == {} and res.withdrawal == {} and seen == []
    taker, wd = merged(cfg, res)
    assert taker == cfg.taker_fee


def test_endpoint_failure_falls_back_and_never_leaks_secret():
    cfg, res, _ = _run(ENV, status=404)
    assert "valr:BTCZAR" not in res.taker
    assert any(n.startswith("FAIL  valr trade fees") for n in res.notes)
    assert not any("vsecret123" in n for n in res.notes)
    assert "luno:XBTZAR" in res.taker                           # other exchanges unaffected


def test_per_market_fee_overrides_exchange_default():
    mk = {"a:BTCZAR": Market("a:BTCZAR", "a", "BTCZAR", "BTC", "ZAR"),
          "b:BTCZAR": Market("b:BTCZAR", "b", "BTCZAR", "BTC", "ZAR")}
    r = Route("r", (Step("a:BTCZAR", "buy"), Step("b:BTCZAR", "sell")))
    books = {"a:BTCZAR": book("a:BTCZAR", 999, 1000), "b:BTCZAR": book("b:BTCZAR", 1020, 1021)}
    base = simulate(r, mk, books, {"a": 0.001, "b": 0.001}, {}, 100, False)
    cheaper = simulate(r, mk, books, {"a": 0.001, "b": 0.001, "a:BTCZAR": 0.0}, {}, 100, False)
    assert cheaper > base


def test_load_env(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment\nVALR_API_KEY=abc\nLUNO_API_SECRET='x y'\nBAD LINE\n")
    monkeypatch.delenv("VALR_API_KEY", raising=False)
    env = load_env(p)
    assert env["VALR_API_KEY"] == "abc" and env["LUNO_API_SECRET"] == "x y"
