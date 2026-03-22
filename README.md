# Options Selling Algo — Dhan Platform

Automated options selling bot for **NIFTY** and **BANKNIFTY** on the NSE using the [Dhan](https://dhan.co) platform via the `Dhan-Tradehull` Python library.

---

## Features

- **Paper trading mode** — test with simulated orders before going live
- **Pluggable strategy framework** — drop in new strategies without touching the core bot
- **CSV trade tracker** — all trades written to lightweight CSV files, no Excel dependency
- **Interactive HTML report** — cumulative charts and tables generated on demand, opens in any browser
- **Position resumption** — on restart, bot automatically picks up open positions from previous runs
- **Risk management** — daily loss limit, max open positions, min premium filters
- **Adjustment engine** — strategies can roll legs mid-trade without closing the position

---

## Project Structure

```
options_bot.py                   # Main entry point — run this
broker.py                        # Dhan-Tradehull API wrapper
config.py                        # All settings and parameters
strategies.py                    # Trade dataclasses + strategy registry
csv_tracker.py                   # CSV file I/O for all trade data
excel_tracker.py                 # (Legacy) Live Excel I/O via xlwings
generate_report.py               # Generate interactive HTML report from CSVs
strategy_base.py                 # Abstract base class for all strategies
strategy_shortStrangle.py        # Strategy: plain short strangle
strategy_shortStrangle_Adjust.py # Strategy: short strangle with adjustment logic
config.json.example              # Credential template — copy to config.json
requirements.txt                 # Python dependencies

data/                            # Created automatically at runtime
  open_positions.csv             # All positions (OPEN and CLOSED)
  trade_history.csv              # Append-only closed trade log
  daily_summary.csv              # Append-only daily P&L per strategy

logs/                            # Created automatically at runtime
  options_bot.log                # Rotating bot log
```

---

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

> **Note:** `ta-lib` requires the TA-Lib C library to be installed first.
> - Windows: download the `.whl` from [here](https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib)
> - Mac: `brew install ta-lib`
> - Linux: `sudo apt-get install ta-lib`

### 3. Configure credentials
Copy the example config and fill in your Dhan credentials:
```bash
cp config.json.example config.json
```
Edit `config.json`:
```json
{
    "dhan_config": {
        "client_code": "YOUR_CLIENT_CODE",
        "access_token": "YOUR_ACCESS_TOKEN",
        "remark": "Primary Trading Account"
    },
    "telegram_bot":{
        "chat_id" : CHAT_ID,
        "bot_token" : "YOUR_TELGRAM_BOT_TOKEN",
        "remark": "Telegram bot information for notifications"
    }
}
```
Get these from your [Dhan API portal](https://my.dhan.co).

### 4. Configure the bot
Edit `config.py` to set your trading parameters:

| Setting | Default | Description |
|---|---|---|
| `ACTIVE_STRATEGY` | `"shortStrangle"` | Strategy to run |
| `PAPER_TRADING` | `True` | Simulate orders — set `False` for live |
| `INSTRUMENTS` | `["NIFTY", "BANKNIFTY"]` | Instruments to trade |
| `EXPIRY_INDEX` | `0` | `0`=current expiry, `1`=next, etc. |
| `NUM_STRIKES` | `30` | Strikes either side of ATM to fetch |
| `TARGET_DELTA` | `0.20` | Delta of strikes to sell |
| `MAX_DELTA` | `0.25` | Reject strike if delta exceeds this |
| `MAX_LOTS_PER_TRADE` | `2` | Lots per trade |
| `MAX_OPEN_POSITIONS` | `6` | Max concurrent positions |
| `MAX_DAILY_LOSS_INR` | `10000` | Kill-switch daily loss limit (₹) |
| `MIN_PREMIUM` | `50` | Minimum combined premium to enter (₹) |
| `PROFIT_TARGET_PCT` | `0.70` | Close at 70% of premium collected |
| `STOP_LOSS_MULTIPLIER` | `1.5` | Close if premium rises to 1.5× collected |
| `MARKET_OPEN` | `"09:30"` | Start scanning (IST) |
| `MARKET_CLOSE` | `"15:20"` | Stop new entries (IST) |
| `SCAN_INTERVAL_SECONDS` | `120` | Seconds between scan cycles |
| `LOG_LEVEL` | `"INFO"` | Log verbosity: DEBUG / INFO / WARNING / ERROR |

---

## Running the Bot

```bash
python options_bot.py
```

The bot will:
1. Connect to Dhan and verify credentials
2. Load any previously open positions from `data/open_positions.csv`
3. Enter the main scan loop — checking for entries and monitoring open positions each cycle
4. Write all trade data to `data/*.csv` in real time
5. On `Ctrl+C` — log a clean shutdown message and exit

> **Paper trading is on by default** (`PAPER_TRADING = True` in `config.py`).  
> Set it to `False` only when you are ready to trade with real money.

### Switching from paper to live
1. Set `PAPER_TRADING = False` in `config.py`
2. Restart the bot — paper positions are automatically ignored on resume
3. The bot will only pick up positions that were opened in `LIVE` mode

---

## Generating the Report

The report reads all CSVs and produces a single self-contained HTML file.  
Run it any time — while the bot is running or after market hours.

```bash
# Default: report_YYYY-MM-DD.html in current directory
python generate_report.py

# Specify output path
python generate_report.py --out reports/week1.html

# Filter by mode
python generate_report.py --mode PAPER
python generate_report.py --mode LIVE

# Filter by strategy
python generate_report.py --strategy shortStrangle_Adjust

# Combine filters
python generate_report.py --mode LIVE --strategy shortStrangle --out reports/live_strangle.html
```

Open the generated `.html` file in any browser — no server needed.

### Report sections

| Section | Contents |
|---|---|
| Summary cards | Total P&L, trades, win rate, avg P&L, best/worst trade, open positions |
| Strategy Comparison | Side-by-side bar charts + table comparing all strategies |
| Equity Curve | Cumulative P&L over time, one line per strategy (interactive) |
| Win/Loss donut | Overall win/loss ratio |
| Daily P&L | Bar chart of net P&L per day |
| P&L Distribution | Histogram of individual trade results |
| Open Positions | Table of currently open trades |
| Trade History | Full searchable table of all closed trades |

Since `trade_history.csv` and `daily_summary.csv` are append-only, every report you generate always shows the **full cumulative history** from day one.

---

## Strategies

### `shortStrangle`
Sells an OTM CE and OTM PE near `TARGET_DELTA` using the highest OI strikes.
- **Entry:** both CE and PE available near target delta, combined premium above `MIN_PREMIUM`
- **Exit:** 70% of max profit collected, or stop loss hit, or expiry day before 3 PM

### `shortStrangle_Adjust`
Same entry as `shortStrangle`, with an active adjustment engine:
- **Adjustment trigger:** `|CE_premium - PE_premium| > 40%` of combined entry premium
- **Action:** close the profitable leg, re-sell at a strike whose premium matches the losing leg
- **Strike guard:** CE and PE strikes converge inward only — once they meet (straddle), no further adjustments
- **Exit:** 70% of max profit, or Friday 3:16 PM (force-close before weekly expiry)

To switch strategy, change `ACTIVE_STRATEGY` in `config.py`:
```python
ACTIVE_STRATEGY = "shortStrangle_Adjust"
```

### Adding a new strategy
1. Copy `strategy_shortStrangle.py` → `strategy_myStrategy.py`
2. Set `NAME = "myStrategy"` in the class
3. Implement the three required methods:
   - `entry_criteria(context)` — return `None` to skip or `EntrySignal(legs=[...])` to enter
   - `exit_criteria(context)` — return `(False, "")` to hold or `(True, "reason")` to close
   - `adjustment_done(context)` — return `False` for normal close or `True` if adjustment applied
4. Register it in `strategies.py` inside `_build_registry()`
5. Set `ACTIVE_STRATEGY = "myStrategy"` in `config.py`

The `context` dict passed to each method contains:
```python
{
    "instrument":    "NIFTY",        # str
    "atm_strike":    24500,           # int — ATM from option chain
    "expiry":        "27MAR2025",     # str
    "option_chain":  <DataFrame>,     # CE/PE deltas, OI, LTP, security IDs
    "lot_size":      50,              # int
    "open_trades":   [...],           # list of Trade objects
    "closed_trades": [...],
    "broker":        <DhanBroker>,    # for any extra API calls
    # exit/adjustment methods also receive:
    "trade":         <Trade>,         # the trade being evaluated
    "ltps":          {symbol: ltp},   # current live prices
    "exit_reason":   "...",           # adjustment_done only
}
```

---

## CSV Data Files

All data lives in the `data/` folder, created automatically on first run.

| File | Description | Write mode |
|---|---|---|
| `open_positions.csv` | Every trade ever opened (OPEN and CLOSED rows) | Overwrite on each change |
| `trade_history.csv` | One row per closed trade with full P&L breakdown | Append-only |
| `daily_summary.csv` | One row per day per strategy with aggregated stats | Append-only (upsert by date+strategy) |

These files are the source of truth for the report. They accumulate over time across all sessions.

---

## ⚠️ Disclaimer

This software is for **educational purposes only**. Options trading involves significant financial risk. Always test thoroughly in paper trading mode before using real capital. The authors are not responsible for any financial losses.

---

## License

MIT
