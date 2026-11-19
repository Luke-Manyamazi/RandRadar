import asyncio
import math
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

import analyze
from randradar import config as config_mod
from randradar.books import BookError, OrderBook, parse_binance, parse_luno, parse_valr
from randradar.exchanges import BookFetcher, MarketHealth
from randradar.routes import Market, Route, Step, fill_buy, fill_sell, gross, simulate
from randradar.runner import Logger
from randradar.store import Store

ROOT = Path(__file__).resolve().parents[1]


def book(mid, bid, ask, qty=10.0, levels=1, step=0.0):
    bids = [(bid - i * step, qty) for i in range(levels)]
    asks = [(ask + i * step, qty) for i in range(levels)]
    return OrderBook(mid, bids, asks, time.time())


MKT = {
    "a:BTCZAR": Market("a:BTCZAR", "a", "BTCZAR", "BTC", "ZAR"),
    "b:BTCZAR": Market("b:BTCZAR", "b", "BTCZAR", "BTC", "ZAR"),
}
ROUTE = Route("a_to_b", (Step("a:BTCZAR", "buy"), Step("b:BTCZAR", "sell")))


# ---------- parsers (payload shapes from each exchange's public docs)

def test_parse_valr():
    p = {"Asks": [{"side": "sell", "quantity": "0.5", "price": "1000100", "currencyPair": "BTCZAR", "orderCount": 1}],
         "Bids": [{"side": "buy", "quantity": "0.2", "price": "1000000", "currencyPair": "BTCZAR", "orderCount": 2}]}
    b = parse_valr("valr:BTCZAR", p, 0)
    assert (b.best_bid, b.best_ask) == (1000000, 1000100)


def test_parse_luno_sorts_and_drops_zero():
    p = {"timestamp": 1, "asks": [{"price": "102", "volume": "1"}, {"price": "101", "volume": "0"},
                                  {"price": "101.5", "volume": "2"}],
         "bids": [{"price": "99", "volume": "1"}, {"price": "100", "volume": "1"}]}
    b = parse_luno("luno:XBTZAR", p, 0)
    assert b.best_ask == 101.5 and b.best_bid == 100


def test_parse_binance():
    b = parse_binance("binance:BTCUSDT", {"lastUpdateId": 1, "bids": [["60000.0", "1.0"]], "asks": [["60001.0", "2"]]}, 0)
    assert b.best_ask == 60001.0


def test_crossed_or_empty_book_rejected():
    with pytest.raises(BookError):
        parse_binance("x", {"bids": [["101", "1"]], "asks": [["100", "1"]]}, 0)
    with pytest.raises(BookError):
        parse_luno("x", {"bids": [], "asks": [{"price": "1", "volume": "1"}]}, 0)


# ---------- order book walking

def test_fill_buy_walks_levels():
    asks = [(100.0, 1.0), (110.0, 1.0)]
    assert fill_buy(asks, 100) == pytest.approx(1.0)
    assert fill_buy(asks, 155) == pytest.approx(1.5)   # 100 for first BTC, 55 buys half of second
    assert fill_buy(asks, 1000) is None                 # book too thin


def test_fill_sell_walks_levels():
    bids = [(100.0, 1.0), (90.0, 1.0)]
    assert fill_sell(bids, 1.5) == pytest.approx(145.0)
    assert fill_sell(bids, 5) is None


def test_depth_within():
    b = OrderBook("x", [(100, 1), (99.5, 1), (98, 1)], [(101, 1), (101.9, 1), (103, 1)], 0)
    assert b.depth_within(1, "bid") == pytest.approx(199.5)
    assert b.depth_within(1, "ask") == pytest.approx(202.9)


# ---------- spread maths with fees

def test_net_spread_matches_hand_calculation():
    books = {"a:BTCZAR": book("a:BTCZAR", 999_000, 1_000_000),
             "b:BTCZAR": book("b:BTCZAR", 1_020_000, 1_021_000)}
    fee = {"a": 0.001, "b": 0.001}
    net = simulate(ROUTE, MKT, books, fee, {}, 20_000, include_transfers=False)
    # 20000/1e6 = 0.02 BTC * 0.999 = 0.01998 BTC -> *1.02e6 = 20379.6 * 0.999 = 20359.2204
    assert net == pytest.approx(20359.2204 / 20000 - 1)
    assert gross(ROUTE, books) == pytest.approx(0.02)


def test_transfer_fees_reduce_net():
    books = {"a:BTCZAR": book("a:BTCZAR", 999_000, 1_000_000),
             "b:BTCZAR": book("b:BTCZAR", 1_020_000, 1_021_000)}
    fee = {"a": 0.001, "b": 0.001}
    wd = {"a": {"BTC": 0.0001}, "b": {"ZAR": 10.0}}
    inv = simulate(ROUTE, MKT, books, fee, wd, 20_000, False)
    trf = simulate(ROUTE, MKT, books, fee, wd, 20_000, True)
    # BTC fee 0.0001 * 1.02e6 * 0.999 ~ R101.9, plus R10 bank fee to bring rand home
    assert (inv - trf) * 20_000 == pytest.approx(0.0001 * 1_020_000 * 0.999 + 10, rel=1e-6)


def test_no_opportunity_is_negative_after_fees():
    books = {"a:BTCZAR": book("a:BTCZAR", 999_000, 1_000_000),
             "b:BTCZAR": book("b:BTCZAR", 999_500, 1_000_500)}
    assert simulate(ROUTE, MKT, books, {"a": 0.001, "b": 0.001}, {}, 20_000, False) < 0


def test_slippage_is_measured_from_depth():
    thin = {"a:BTCZAR": book("a:BTCZAR", 999_000, 1_000_000, qty=0.005, levels=10, step=2_000),
            "b:BTCZAR": book("b:BTCZAR", 1_020_000, 1_021_000, qty=10)}
    deep = {"a:BTCZAR": book("a:BTCZAR", 999_000, 1_000_000, qty=10),
            "b:BTCZAR": book("b:BTCZAR", 1_020_000, 1_021_000, qty=10)}
    f = {"a": 0.001, "b": 0.001}
    assert simulate(ROUTE, MKT, thin, f, {}, 20_000, False) < simulate(ROUTE, MKT, deep, f, {}, 20_000, False)


# ---------- config

def test_shipped_config_is_valid():
    cfg = config_mod.load(ROOT / "config.yaml")
    assert len(cfg.routes) >= 4 and cfg.taker_fee["valr"] == pytest.approx(0.001)


def test_route_that_does_not_close_in_zar_is_rejected(tmp_path):
    bad = (ROOT / "config.yaml").read_text().replace(
        'btc_valr_to_luno:   ["buy valr:BTCZAR", "sell luno:XBTZAR"]',
        'btc_valr_to_luno:   ["buy valr:BTCZAR"]')
    p = tmp_path / "c.yaml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="must end in ZAR"):
        config_mod.load(p)


# ---------- circuit breaker / backoff

def test_breaker_backoff_doubles_and_caps():
    h = MarketHealth()
    pauses = []
    for _ in range(12):
        h.record_fail(1000.0, "boom")
        pauses.append(max(0.0, h.paused_until - 1000.0))
    assert pauses[:2] == [0, 0]               # first two failures: no pause
    assert pauses[2:6] == [1, 2, 4, 8]        # then 1s, 2s, 4s, 8s
    assert max(pauses) == 60                  # capped
    h.record_ok(2000.0)
    assert h.consecutive_failures == 0 and h.paused_until == 0


def test_fetcher_handles_404_429_and_garbage():
    calls = {"n": 0}

    def handler(req: httpx.Request):
        calls["n"] += 1
        if "BADPAIR" in str(req.url):
            return httpx.Response(404, text="not found")
        if "SLOW" in str(req.url):
            return httpx.Response(429, headers={"Retry-After": "5"})
        if "JUNK" in str(req.url):
            return httpx.Response(200, text="<html>maintenance</html>")
        return httpx.Response(200, json={"lastUpdateId": 1, "bids": [["1", "1"]], "asks": [["2", "1"]]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            f = BookFetcher(http)
            ok = await f.fetch(Market("binance:BTCUSDT", "binance", "BTCUSDT", "BTC", "USDT"))
            bad = await f.fetch(Market("binance:BADPAIR", "binance", "BADPAIR", "X", "Y"))
            slow = await f.fetch(Market("binance:SLOW", "binance", "SLOW", "X", "Y"))
            junk = await f.fetch(Market("binance:JUNK", "binance", "JUNK", "X", "Y"))
            n_before = calls["n"]
            again = await f.fetch(Market("binance:BADPAIR", "binance", "BADPAIR", "X", "Y"))
            return ok, bad, slow, junk, again, f, calls["n"] - n_before

    ok, bad, slow, junk, again, f, extra_calls = asyncio.run(go())
    assert ok is not None and bad is None and slow is None and junk is None
    assert f.health["binance:BADPAIR"].invalid
    assert again is None and extra_calls == 0          # paused, so no request was made
    assert f.health["binance:SLOW"].paused_until > time.time()


# ---------- end-to-end loop with a fake exchange

def test_logger_end_to_end_and_report(tmp_path):
    cfg = config_mod.load(ROOT / "config.yaml")
    cfg.db_path = str(tmp_path / "t.sqlite")
    cfg.interval_s = 0.01
    cfg.stale_after_s = 0.001   # any missed fetch counts as down, to exercise recovery

    tick = {"n": 0}
    base = {"valr:BTCZAR": 1_100_000, "luno:XBTZAR": 1_100_000, "valr:USDTZAR": 17.8,
            "luno:USDTZAR": 17.8, "binance:BTCUSDT": 61_000}

    async def fake_fetch(m: Market):
        n = tick["n"]
        if m.market_id == "luno:USDTZAR" and n % 7 == 3:
            return None                           # simulate an outage
        mid = base[m.market_id]
        if m.market_id == "luno:XBTZAR" and 10 <= n < 30:
            mid *= 1.02                           # a 2% gap for 20 ticks
        spread = mid * 0.0002
        return book(m.market_id, mid - spread, mid + spread, qty=5000 if m.base == "USDT" else 2,
                    levels=5, step=mid * 0.0005)

    async def fake_fx():
        return 17.6

    async def go():
        lg = Logger(cfg, Store(cfg.db_path), fake_fetch, fake_fx)
        orig = lg.tick

        async def counted():
            await orig()
            tick["n"] += 1
        lg.tick = counted
        await lg.run(max_ticks=60)

    asyncio.run(go())
    db = sqlite3.connect(cfg.db_path)
    n_spreads = db.execute("SELECT COUNT(*) FROM spreads").fetchone()[0]
    best = db.execute("SELECT MAX(net_inventory_pct) FROM spreads WHERE route='btc_valr_to_luno'").fetchone()[0]
    prem = db.execute("SELECT AVG(usdt_premium_pct) FROM spreads").fetchone()[0]
    assert n_spreads > 300
    assert 1.5 < best < 2.0                       # 2% gap minus fees and spread
    assert prem == pytest.approx((17.8 / 17.6 - 1) * 100, rel=1e-3)
    assert db.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%recovered%'").fetchone()[0] > 0

    report = analyze.analyze(cfg.db_path, None, 1.2, 0.05, 0.01)
    assert "alerts/day" in report and "btc_valr_to_luno" in report
    assert "| usdt_valr_to_luno | 60 | 0.0% |" in report       # deep USDT books are fillable


def test_episode_detection():
    rows = [(0, 0.1), (3, 1.3), (6, 1.5), (9, 1.4), (12, 1.3), (15, 0.2), (18, 1.3), (60, 1.3)]
    eps = analyze.episodes(rows, 1.2, max_gap=12)
    assert [(e[0], e[1], e[2]) for e in eps] == [(3, 12, 1.5), (18, 18, 1.3), (60, 60, 1.3)]
