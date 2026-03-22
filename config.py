# ─────────────────────────────────────────────────────────────────
#  Options Selling Bot — Configuration
#  Platform : Dhan (via Dhan-Tradehull)
#  Instruments: NIFTY | BANKNIFTY (NSE)
# ─────────────────────────────────────────────────────────────────

# ── Instruments ───────────────────────────────────────────────────
INSTRUMENTS = ["BANKNIFTY"]

# ── Option Chain ──────────────────────────────────────────────────
# expiry index passed to get_option_chain():
#   0 = current/nearest expiry (weekly or monthly)
#   1 = next expiry
#   2 = 2 expiries out, etc.
EXPIRY_INDEX  = 0
NUM_STRIKES   = 30    # number of strikes either side of ATM to fetch

# ── Strategy Parameters ───────────────────────────────────────────
TARGET_DELTA          = 0.18   # Sell strikes near this delta
MAX_DELTA             = 0.20   # Reject strike if delta exceeds this
MIN_DAYS_TO_EXPIRY    = 1      # Avoid same-day expiry entries
MAX_DAYS_TO_EXPIRY    = 7      # Weekly expiry window
MIN_PREMIUM           = 50     # Minimum premium (Rs.) to collect per lot
PROFIT_TARGET_PCT     = 0.70   # Close at 70% of premium collected
STOP_LOSS_MULTIPLIER  = 1.5    # Close if premium rises to 1.5x collected

# ── Position Sizing ───────────────────────────────────────────────
MAX_LOTS_PER_TRADE    = 2      # Max lots per single order
MAX_OPEN_POSITIONS    = 6      # Total open positions at one time
MAX_DAILY_LOSS_INR    = 10000  # Hard stop for the day (Rs.)

# ── Active Strategy ───────────────────────────────────────────────
# Name must match BaseStrategy.NAME in the corresponding strategy file.
# To add a new strategy: create strategy_<name>.py, register it in
# strategies.py _build_registry(), then set the name here.
#    shortStrangle
#    shortStrangle_Adjust
ACTIVE_STRATEGY = "shortStrangle_Adjust"   # currently active strategy

# ── Excel Tracker ─────────────────────────────────────────────────
EXCEL_FILE = "options_tracker.xlsx"

# ── Timing (IST) ──────────────────────────────────────────────────
MARKET_OPEN            = "09:25"
MARKET_CLOSE           = "15:16"
SCAN_INTERVAL_SECONDS  = 120

# ── Paper Trading ─────────────────────────────────────────────────
# True  → simulation mode: no real orders placed, random IDs used,
#          SL/TG triggers simulated via generate_bool().
# False → live mode: real orders sent to Dhan. Use with caution.
PAPER_TRADING = True

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL = "INFO"                    # DEBUG | INFO | WARNING | ERROR
LOG_FILE  = "logs/options_bot.log"
