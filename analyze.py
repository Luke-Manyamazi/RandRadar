"""Study report: how often is there a real, tradeable gap?

    python analyze.py                       # all data
    python analyze.py --days 7              # last 7 days
    python analyze.py --alert-threshold 1.2 --persist 10

Writes reports/study_<date>.md and prints it.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SAST = timezone(timedelta(hours=2))
THRESHOLDS = [0.0, 0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0]


def episodes(rows: list[tuple[float, float | None]], threshold: float, max_gap: float):
    """Group consecutive samples with value >= threshold into episodes.

    rows: (ts, value) sorted by ts. A gap longer than max_gap (logger down) ends an episode.
    Returns list of (start_ts, end_ts, peak_value, n_samples).
    """
    out, cur = [], None
    prev_ts = None
    for ts, v in rows:
        gap_break = prev_ts is not None and ts - prev_ts > max_gap
        hit = v is not None and v >= threshold
        if cur and (not hit or gap_break):
            out.append(tuple(cur))
            cur = None
        if hit:
            if cur is None:
                cur = [ts, ts, v, 1]
            else:
                cur[1], cur[2], cur[3] = ts, max(cur[2], v), cur[3] + 1
        prev_ts = ts
    if cur:
        out.append(tuple(cur))
    return out


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[k]


def fmt(x, nd=2, suffix="%"):
    return "-" if x is None else f"{x:.{nd}f}{suffix}"


def analyze(db_path: str, days: float | None, alert_thr: float, persist_s: float, interval_s: float) -> str:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    since = time.time() - days * 86400 if days else 0
    rows = db.execute(
        "SELECT ts, route, gross_pct, net_inventory_pct, net_transfer_pct, usdt_premium_pct, notional_zar "
        "FROM spreads WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    if not rows:
        return "No data yet. Run `python run.py` for a while first."

    t0, t1 = rows[0][0], rows[-1][0]
    span_days = max((t1 - t0) / 86400, 1e-9)
    notional = rows[-1][6]
    max_gap = interval_s * 4

    by_route: dict[str, list] = defaultdict(list)
    premiums = []
    for ts, route, g, ni, nt, prem, _ in rows:
        by_route[route].append((ts, g, ni, nt))
        if prem is not None:
            premiums.append(prem)

    L = []
    L.append("# RandRadar study report")
    L.append("")
    L.append(f"- Period: {datetime.fromtimestamp(t0, SAST):%Y-%m-%d %H:%M} to "
             f"{datetime.fromtimestamp(t1, SAST):%Y-%m-%d %H:%M} SAST ({span_days:.1f} days)")
    L.append(f"- Simulated trade size: R{notional:,.0f} walked through the live order book "
             f"(so slippage and liquidity are measured, not guessed)")
    L.append(f"- Alert rule tested: net >= {alert_thr}% lasting >= {persist_s:.0f}s")
    L.append("- **Inventory** = funds already sitting on each exchange (trading fees only). "
             "**Transfer** = coins moved every trade (withdrawal/network/bank fees too).")
    L.append("")

    # ---- headline verdict
    total_inv = total_trf = 0
    verdict_rows = []
    for route, rs in sorted(by_route.items()):
        inv = [e for e in episodes([(r[0], r[2]) for r in rs], alert_thr, max_gap) if e[1] - e[0] >= persist_s]
        trf = [e for e in episodes([(r[0], r[3]) for r in rs], alert_thr, max_gap) if e[1] - e[0] >= persist_s]
        total_inv += len(inv)
        total_trf += len(trf)
        verdict_rows.append((route, len(inv), len(trf)))
    L.append("## Headline: would-be alerts per day under your rule")
    L.append("")
    L.append(f"- Inventory model: **{total_inv / span_days:.2f} alerts/day** ({total_inv} total)")
    L.append(f"- Transfer model: **{total_trf / span_days:.2f} alerts/day** ({total_trf} total)")
    if span_days < 7:
        L.append(f"- Warning: only {span_days * 24:.1f} hours of data. Per-day figures are not reliable "
                 "until you have at least 7 days, ideally 28 (weekends and month-end behave differently).")
    L.append("")
    L.append("| Route | Inventory alerts/day | Transfer alerts/day |")
    L.append("|---|---:|---:|")
    for route, a, b in verdict_rows:
        L.append(f"| {route} | {a / span_days:.2f} | {b / span_days:.2f} |")
    L.append("")

    # ---- distribution per route
    L.append("## Net spread distribution (inventory model)")
    L.append("")
    L.append("| Route | Samples | Unfillable | Gross median | Net median | Net p95 | Net max | " +
             " | ".join(f"% time >= {t}%" for t in THRESHOLDS[1:]) + " |")
    L.append("|---|---:|---:|---:|---:|---:|---:|" + "---:|" * (len(THRESHOLDS) - 1))
    for route, rs in sorted(by_route.items()):
        nets = [r[2] for r in rs if r[2] is not None]
        unfill = sum(1 for r in rs if r[2] is None) / len(rs) * 100
        gross = [r[1] for r in rs]
        shares = [fmt(sum(1 for v in nets if v >= t) / len(rs) * 100, 2) for t in THRESHOLDS[1:]]
        L.append(f"| {route} | {len(rs):,} | {fmt(unfill, 1)} | {fmt(statistics.median(gross), 3)} | "
                 f"{fmt(statistics.median(nets) if nets else None, 3)} | {fmt(pct(nets, 95), 3)} | "
                 f"{fmt(max(nets) if nets else None, 3)} | " + " | ".join(shares) + " |")
    L.append("")

    # ---- persistence
    L.append("## How long do gaps last? (inventory model)")
    L.append("")
    L.append("| Route | Threshold | Episodes | Lasting >= persist | Median duration | p90 duration | Best peak |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for route, rs in sorted(by_route.items()):
        series = [(r[0], r[2]) for r in rs]
        for t in (0.3, 0.5, 1.0, alert_thr):
            eps = episodes(series, t, max_gap)
            if not eps:
                continue
            durs = [e[1] - e[0] for e in eps]
            lasting = sum(1 for d in durs if d >= persist_s)
            L.append(f"| {route} | {t}% | {len(eps)} | {lasting} | {fmt(statistics.median(durs), 0, 's')} | "
                     f"{fmt(pct(durs, 90), 0, 's')} | {fmt(max(e[2] for e in eps), 2)} |")
    L.append("")

    # ---- time of day
    L.append(f"## When do gaps >= 0.5% appear? (hour of day, SAST, inventory model)")
    L.append("")
    hours = Counter()
    for rs in by_route.values():
        for e in episodes([(r[0], r[2]) for r in rs], 0.5, max_gap):
            hours[datetime.fromtimestamp(e[0], SAST).hour] += 1
    if hours:
        peak = max(hours.values())
        for h in range(24):
            n = hours.get(h, 0)
            L.append(f"    {h:02d}:00  {'#' * round(n / peak * 30):<30} {n}")
    else:
        L.append("No episodes >= 0.5% yet.")
    L.append("")

    # ---- USDT premium
    L.append("## USDT premium vs official USD/ZAR")
    L.append("")
    if premiums:
        L.append(f"- Mean {statistics.mean(premiums):+.3f}% | median {statistics.median(premiums):+.3f}% | "
                 f"min {min(premiums):+.3f}% | max {max(premiums):+.3f}% ({len(premiums):,} samples)")
    else:
        L.append("- No premium data (FX reference or USDT/ZAR market unavailable).")
    L.append("")

    # ---- data quality
    L.append("## Data quality")
    L.append("")
    expected = (t1 - t0) / interval_s if t1 > t0 else 1
    L.append("| Market | Quotes | Coverage |")
    L.append("|---|---:|---:|")
    for market, n in db.execute("SELECT market, COUNT(*) FROM quotes WHERE ts >= ? GROUP BY market ORDER BY market",
                                (since,)):
        L.append(f"| {market} | {n:,} | {min(n / expected * 100, 100):.1f}% |")
    errs = db.execute("SELECT level, COUNT(*) FROM events WHERE ts >= ? GROUP BY level", (since,)).fetchall()
    L.append("")
    L.append("- Events: " + (", ".join(f"{lvl} {n}" for lvl, n in errs) or "none"))
    L.append("")
    L.append("_Research output from public market data. Not financial advice._")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/arb.sqlite")
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--alert-threshold", type=float, default=1.2)
    ap.add_argument("--persist", type=float, default=10.0, help="seconds a gap must last")
    ap.add_argument("--interval", type=float, default=3.0, help="logger poll interval, seconds")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()

    report = analyze(a.db, a.days, a.alert_threshold, a.persist, a.interval)
    print(report)
    Path(a.out).mkdir(exist_ok=True)
    path = Path(a.out) / f"study_{datetime.now(SAST):%Y%m%d_%H%M}.md"
    path.write_text(report)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
