# dhan-algo — Options Selling Bot

An automated options selling bot for **NSE index options** (NIFTY, BANKNIFTY, FINNIFTY)
and **MCX commodity options** (CRUDEOIL, GOLD, SILVER) on the [Dhan](https://dhan.co)
platform, built using the `Dhan-Tradehull` Python library.

Designed to run on Windows, a VPS, or inside a Docker container.

---

## Features

| | |
|---|---|
| 📄 **Paper trading mode** | Simulate orders with fake IDs before going live |
| 🔌 **Pluggable strategy framework** | Drop in new strategies without touching the core bot |
| 🌐 **Multi-exchange support** | Trade NSE index options and MCX commodity options simultaneously |
| 🔄 **Position resumption** | On restart, open positions are reloaded from CSV automatically |
| 📊 **CSV trade tracker** | All trade data written to lightweight CSV files — no Excel dependency |
| 📈 **Interactive HTML report** | Cumulative charts and tables generated on demand, opens in any browser |
| ⚖️ **Adjustment engine** | Strategies can roll individual legs mid-trade without closing the position |
| 🛡️ **Risk management** | Daily loss limit, max open positions, min premium filter, paper/live guard |

---

## Project Structure

```
├── options_bot.py                    # Main entry point — run this
├── broker.py                         # Dhan-Tradehull API wrapper
├── config.py                         # All bot settings and parameters
├── strategies.py                     # Trade/OptionLeg dataclasses + strategy registry
│
├── strategy_base.py                  # Abstract base class for all strategies
├── strategy_shortStrangle.py         # Strategy: plain short strangle
├── strategy_shortStrangle_Adjust.py  # Strategy: short strangle with leg-rolling adjustment
│
├── csv_tracker.py                    # Writes all trade data to CSV files
├── excel_tracker.py                  # (Legacy) Live Excel I/O via xlwings
├── generate_report.py                # Generates interactive HTML report from CSVs
│
├── config.json.example               # Credential template — copy to config.json
├── requirements.txt                  # Python dependencies
│
├── data/                             # Auto-created at runtime
│   ├── open_positions.csv            # All positions (OPEN and CLOSED)
│   ├── trade_history.csv             # Append-only closed trade log
│   ├── daily_summary.csv             # Daily P&L per strategy
│   └── adjustments.csv              # Leg roll history with booked P&L
│
└── logs/                             # Auto-created at runtime
    └── options_bot.log
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/ujjwal82nl/dhan-algo.git
cd dhan-algo
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **`ta-lib` requires the TA-Lib C library first:**
> - **Windows:** download the `.whl` from [here](https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib)
> - **Mac:** `brew install ta-lib`
> - **Linux / Docker:** `apt-get install -y libta-lib-dev`

### 3. Set up credentials

Copy the example and fill in your Dhan details:

```bash
cp config.json.example config.json
```

```json
{
    "dhan_config": {
        "client_code": "YOUR_CLIENT_CODE",
        "access_token": "YOUR_ACCESS_TOKEN"
    }
}
```

Get your credentials from the [Dhan API portal](https://my.dhan.co).

> `config.json` is in `.gitignore` and will never be committed.

### 4. Set the credentials file path

In `config.py`, point `CONFIG_FILE` to wherever your `config.json` lives:

```python
# Local / Windows development
CONFIG_FILE = "C:/path/to/config.json"

# Docker / VPS
CONFIG_FILE = "/app/config.json"
```

### 5. Configure instruments

`config.INSTRUMENTS` is a dict mapping each instrument name to its exchange string.
Edit it to add or remove instruments — no other file needs to change:

```python
INSTRUMENTS = {
    "BANKNIFTY": "INDEX",   # NSE index options
    # "NIFTY":   "INDEX",
    # "FINNIFTY":"INDEX",
    "CRUDEOIL":  "MCX",     # MCX commodity options
    # "GOLD":    "MCX",
    # "SILVER":  "MCX",
}
```

Supported exchange values:

| Exchange | Instruments |
|---|---|
| `"INDEX"` | NIFTY, BANKNIFTY, FINNIFTY and other NSE index options |
| `"NFO"` | NSE stock futures and options |
| `"MCX"` | CRUDEOIL, GOLD, SILVER and other MCX commodity options |

### 6. Configure the bot

Edit `config.py`:

| Setting | Default | Description |
|---|---|---|
| `ACTIVE_STRATEGY` | `"shortStrangle_Adjust"` | Strategy to run |
| `PAPER_TRADING` | `True` | `False` = live trading |
| `EXPIRY_INDEX` | `0` | `0`=current expiry, `1`=next, etc. |
| `NUM_STRIKES` | `30` | Strikes either side of ATM to fetch |
| `TARGET_DELTA` | `0.18` | Delta of strikes to sell |
| `MAX_DELTA` | `0.20` | Reject strike if delta exceeds this |
| `MAX_LOTS_PER_TRADE` | `2` | Lots per trade |
| `MAX_OPEN_POSITIONS` | `6` | Max concurrent positions |
| `MAX_DAILY_LOSS_INR` | `10000` | Hard kill-switch daily loss limit (₹) |
| `MIN_PREMIUM` | `50` | Minimum combined premium to enter (₹) |
| `PROFIT_TARGET_PCT` | `0.70` | Close at 70% of premium collected |
| `STOP_LOSS_MULTIPLIER` | `1.5` | Close if premium rises to 1.5× collected |
| `MARKET_OPEN` | `"05:00"` | Start scanning (IST) — set early for MCX |
| `MARKET_CLOSE` | `"23:40"` | Stop new entries (IST) — set late for MCX |
| `SCAN_INTERVAL_SECONDS` | `120` | Seconds between scan cycles |
| `LOG_LEVEL` | `"INFO"` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Running the Bot

```bash
python options_bot.py
```

On startup the bot will:
1. Connect to Dhan and verify credentials
2. Load any previously open positions from `data/open_positions.csv`
3. Skip paper/live mode mismatches — paper positions are never resumed in live mode
4. Enter the main scan loop — entries, exits, and adjustments each cycle
5. Write all events to `data/*.csv` in real time

Stop with `Ctrl+C` — the bot logs a clean shutdown message.

> **Paper trading is on by default.**
> Set `PAPER_TRADING = False` in `config.py` only when ready to trade real money.
> Start with `MAX_LOTS_PER_TRADE = 1` for the first few live days.

---

## Generating the Report

Reads all CSVs and produces a **single self-contained HTML file**.
Run any time — while the bot is running or after market hours.

```bash
# Default output: report_YYYY-MM-DD.html
python generate_report.py

# Custom output path
python generate_report.py --out reports/week1.html

# Filter by mode
python generate_report.py --mode PAPER
python generate_report.py --mode LIVE

# Filter by strategy
python generate_report.py --strategy shortStrangle_Adjust

# Combine filters
python generate_report.py --mode LIVE --strategy shortStrangle --out reports/live.html
```

Open the `.html` file in any browser — no server or internet required.

### Report contents

| Section | Contents |
|---|---|
| Summary cards | Total P&L, trades, win rate, avg P&L, best/worst trade, open count |
| Strategy Comparison | Side-by-side bar charts + summary table per strategy |
| Equity Curve | Cumulative P&L over time, one line per strategy |
| Win/Loss donut | Overall win/loss ratio |
| Daily P&L | Bar chart per day, colour-coded |
| P&L Distribution | Histogram of individual trade results |
| Open Positions | Table of currently open trades |
| Trade History | Full searchable table of all closed trades |

Since all CSV files are append-only, every report shows **full cumulative history** from day one.

---

## Strategies

### `shortStrangle`

Sells an OTM CE and OTM PE near `TARGET_DELTA`, picking the highest OI strike on each side.

- **Entry:** both legs available near target delta, combined premium ≥ `MIN_PREMIUM`
- **Exit:** 70% of max profit collected, or SL hit, or expiry day before 3 PM
- **Adjustment:** none

### `shortStrangle_Adjust`

Same entry as `shortStrangle`, with an active leg-rolling adjustment engine.

- **Adjustment trigger:** `|CE_ltp - PE_ltp| × quantity > ADJUST_THRESHOLD × total_premium_collected`
  (threshold is `40%` of total credit collected — all values in total Rs.)
- **Action:** close the profitable leg (buy back), re-sell a new strike whose LTP ≈ the losing leg's current LTP
- **Strike guard:** strikes converge inward only — once CE strike == PE strike (straddle), no further adjustments
- **Threshold recalculation:** after each adjustment the threshold is recalculated from the new total premium of all open legs
- **Booked P&L:** each rolled leg's realised profit/loss recorded in `data/adjustments.csv`
- **Exit:** 70% of total credit collected, or Friday 3:16 PM (force-close before weekly expiry)

Switch strategy in `config.py`:

```python
ACTIVE_STRATEGY = "shortStrangle_Adjust"
```

### Adding a new strategy

1. Copy `strategy_shortStrangle.py` → `strategy_myStrategy.py`
2. Set `NAME = "myStrategy"` in the class
3. Implement the three required methods:

```python
def entry_criteria(self, context):
    # Return None to skip, or EntrySignal(legs=[...]) to enter
    ...

def exit_criteria(self, context):
    # Return (False, "") to hold, or (True, "reason") to close
    ...

def adjustment_done(self, context):
    # Return False for normal close, True if adjustment was applied
    ...
```

4. Register in `strategies.py` → `_build_registry()`
5. Set `ACTIVE_STRATEGY = "myStrategy"` in `config.py`

The `context` dict passed to every method:

```python
{
    "instrument":    "BANKNIFTY",       # str
    "exchange":      "INDEX",           # str — from config.INSTRUMENTS
    "atm_strike":    53400,             # int
    "expiry":        "30MAR2026",       # str
    "option_chain":  <DataFrame>,       # CE/PE delta, OI, LTP, security IDs
    "lot_size":      15,                # int
    "open_trades":   [...],             # list of Trade objects
    "closed_trades": [...],
    "broker":        <DhanBroker>,
    # exit / adjustment methods also receive:
    "trade":         <Trade>,
    "ltps":          {symbol: ltp},     # current live prices
    "exit_reason":   "...",             # adjustment_done only
}
```

---

## Data Model

### `OptionLeg` fields

| Field | Type | Description |
|---|---|---|
| `symbol` | str | Trading symbol e.g. `BANKNIFTY 30 MAR 55500 CALL` |
| `instrument` | str | e.g. `BANKNIFTY`, `CRUDEOIL` |
| `exchange` | str | e.g. `INDEX`, `MCX` |
| `expiry` | str | e.g. `30MAR2026` |
| `strike` | int | Strike price |
| `option_type` | str | `CE` or `PE` |
| `lots` | int | Number of lots |
| `quantity` | int | `lots × lot_size` |
| `entry_price` | float | Per-unit fill price (raw) |
| `entry_premium` | float | `entry_price × quantity` (total Rs.) |
| `exit_premium` | float | Per-unit exit price (multiply by qty for exit cost) |

### CSV Data Files

| File | Description | Write mode |
|---|---|---|
| `open_positions.csv` | Every trade opened (OPEN and CLOSED rows) | Overwrite on change |
| `trade_history.csv` | One row per closed trade, full P&L | Append-only |
| `daily_summary.csv` | One row per day per strategy | Upsert by date + strategy |
| `adjustments.csv` | One row per leg roll — closed leg, new leg, booked P&L | Append-only |

---

## Running in Docker

Create a `Dockerfile`:

```dockerfile
FROM python:3.10-slim

RUN apt-get update && apt-get install -y libta-lib-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "options_bot.py"]
```

Mount your credentials at runtime — never bake them into the image:

```bash
docker build -t dhan-algo .
docker run -v /your/local/config.json:/app/config.json \
           -v /your/local/data:/app/data \
           dhan-algo
```

Set in `config.py`:

```python
CONFIG_FILE = "/app/config.json"
```

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Stable, tested, safe to run live |
| `strategy/<name>` | Developing or tuning a strategy |
| `feature/<name>` | New bot features |
| `fix/<name>` | Bug fixes |

**Workflow — always bring your branch up to date before starting work:**

```bash
# Start work on a branch
git checkout -b strategy/shortStrangle-review
git merge main                         # get latest changes first

# Finish and deliver
git checkout main
git merge strategy/shortStrangle-review
git push origin main
```

---

## ⚠️ Disclaimer

This software is for **educational purposes only**. Options trading involves significant financial risk. Always test thoroughly in paper trading mode before using real capital. The authors are not responsible for any financial losses.

---

## License

MIT