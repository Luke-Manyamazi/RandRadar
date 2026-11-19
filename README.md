# RandRadar (beta): Phase 0 study logger

This program answers one question before anything else gets built:

> **How often is there a real, tradeable price gap between VALR, Luno and Binance after fees, and how long does it last?**

It **only reads public prices**. It never trades, never needs API keys, and never touches money.

## What it measures

Every 3 seconds it downloads the order books for:

| Market | Exchange |
|---|---|
| BTC/ZAR, USDT/ZAR | VALR |
| BTC/ZAR (XBTZAR), USDT/ZAR | Luno |
| BTC/USDT | Binance |

It then runs a simulated **R20,000 trade** through 8 routes (each starts and ends in rand):

- **SA-to-SA:** buy BTC or USDT on one SA exchange, sell it on the other.
- **Offshore loops:** for example, ZAR → USDT on VALR → BTC on Binance → back to ZAR on VALR. This is where the "USDT premium" shows up.

For every route it records three numbers:

| Number | Meaning |
|---|---|
| **Gross %** | The headline gap people screenshot: best prices only, no fees. |
| **Net (inventory) %** | After taker fees and real slippage, assuming you already hold money on each exchange. This is how professional arb desks work. |
| **Net (transfer) %** | Same, but you also pay to move coins between exchanges and to withdraw rand to the bank every time. |

Slippage isn't guessed. The R20k is walked through the actual order book levels. If the book can't absorb R20k, the trade is marked **unfillable**, and that's the liquidity filter.

It also logs the **USDT premium**: the VALR USDT/ZAR price compared with the official USD/ZAR rate, which comes from European Central Bank (ECB) data via frankfurter.dev.

## Setup

### Option A: your own computer (good for the first few days)

1. Install Python 3.11 or newer from python.org.
2. Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
python run.py --check        # tests every exchange once: you want all OK
python run.py                # starts logging; leave it running
```

A computer that sleeps leaves gaps in the data. That's fine for a trial, but use a VPS for the real study.

### Option B: a small VPS with Docker (recommended for the 2–4 week study)

A 1 GB VPS is enough. A Johannesburg or Cape Town region is best, because that's where most SA traders sit.

```bash
docker compose build
docker compose run --rm logger python run.py --check
docker compose up -d                 # runs 24/7 and restarts itself after crashes or reboots
docker compose logs -f --tail 50     # watch it; you should see an "alive" line every minute
```

## Before you start: set YOUR fees

Open `config.yaml` and replace the fee placeholders with the figures on your own account's fee pages. Fees differ by tier and by pair type, and they change. Wrong fees make the whole study wrong. Every run saves the fee table it used, so you can still interpret old data later.

## Read the results (do this weekly)

```bash
python analyze.py --days 7
# or with Docker:
docker compose run --rm logger python analyze.py --days 7
```

This prints a report and saves it to `reports/`. It shows:

1. **Alerts per day** under your planned rule (net ≥ 1.2% lasting ≥ 10 seconds), for both the inventory and transfer models.
2. **Distribution:** how much of the time each route sits above 0.3%, 0.5%, 1.0%, 1.2% and so on.
3. **Persistence:** how long gaps actually last. This decides whether a human can act on a Telegram alert at all.
4. **Hour of day** (SAST) when gaps appear.
5. **USDT premium** statistics.
6. **Data quality:** uptime per market. Ignore results from markets with poor coverage.

You can test other rules without collecting new data:

```bash
python analyze.py --alert-threshold 0.8 --persist 30
```

## How to decide after 4 weeks

| What the report shows | What it means |
|---|---|
| Several alerts/day at ≥1.2% that last a minute or more | The paid alert product is viable. Build Phase 1. |
| Gaps exist, but they last only seconds | Humans can't act on alerts. Only an automated executor could, and that's a different (owner-only) business. |
| Gaps exist in the inventory model only | You'd need money pre-funded on both exchanges. Subscribers with small balances can't use the alerts. |
| Hardly anything clears fees | The alert product won't sell. You've saved months of work. |

## Running the tests

```bash
python -m pytest -q
```

The tests cover parsing each exchange's format, order-book walking and slippage, the fee maths (checked against a hand calculation), the circuit breaker and backoff, a full simulated run with an exchange outage and recovery, and the report.

## Reliability built in

- **Per-market circuit breaker:** after 3 failures in a row, it backs off 1s, 2s, 4s… up to 60s. A wrong symbol (HTTP 404) is retried every 10 minutes instead of being hammered.
- **Rate limits:** HTTP 429 and 418 responses are respected via `Retry-After`. Normal load is far below every exchange's limits (Binance uses about 100 of its 6,000 weight per minute).
- **No stale prices:** a route is only calculated when every leg's order book is under 8 seconds old. A market with no fresh data for 30 seconds is logged as DOWN, and its recovery is logged too.
- **Never stops on one bad tick:** errors are written to the `events` table and logging continues. Docker restarts the process if it dies completely.
- **Graceful shutdown:** Ctrl+C or `docker compose down` flushes everything to disk.

Disk use is roughly 15–25 MB per day.

## Not in this phase (on purpose)

Telegram, trading, learning and paid channels are left out until the data says they're worth building.

_Research tool using public market data. Not financial advice._
