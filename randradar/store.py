"""SQLite storage. One file, WAL mode, safe to read with analyze.py while the logger runs."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    ts REAL NOT NULL,
    market TEXT NOT NULL,
    best_bid REAL NOT NULL,
    best_ask REAL NOT NULL,
    bid_depth_quote REAL NOT NULL,   -- quote-currency value within depth_pct of best bid
    ask_depth_quote REAL NOT NULL,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS ix_quotes_ts ON quotes(ts);

CREATE TABLE IF NOT EXISTS spreads (
    ts REAL NOT NULL,
    route TEXT NOT NULL,
    gross_pct REAL NOT NULL,
    net_inventory_pct REAL,          -- NULL when the book could not fill the notional
    net_transfer_pct REAL,
    fillable INTEGER NOT NULL,
    notional_zar REAL NOT NULL,
    usdzar REAL,
    usdt_premium_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_spreads_route_ts ON spreads(route, ts);

CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fee_snapshots (   -- the fee table in force, so old data stays interpretable
    ts REAL NOT NULL,
    fees_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add_quotes(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)", rows)

    def add_spreads(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT INTO spreads VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def event(self, level: str, source: str, message: str) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (time.time(), level, source, message[:1000]))

    def fee_snapshot(self, fees_json: str) -> None:
        self.db.execute("INSERT INTO fee_snapshots VALUES (?,?)", (time.time(), fees_json))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
