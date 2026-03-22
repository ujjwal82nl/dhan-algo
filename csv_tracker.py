from __future__ import annotations

"""
csv_tracker.py — Drop-in replacement for excel_tracker.py.

Writes all trade data to lightweight CSV files in the data/ folder.
Identical public API to excel_tracker.py — swap the import in options_bot.py:
    import csv_tracker as tracker

Files written:
    data/open_positions.csv   — all positions (OPEN and CLOSED), one row per trade
    data/trade_history.csv    — append-only, one row per closed trade
    data/daily_summary.csv    — append-only, one row per day per strategy

Run generate_report.py at any time to produce a cumulative interactive HTML report.
"""

import os
import csv
import logging
from datetime import datetime, date
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DATA_DIR      = Path("data")
OPEN_FILE     = DATA_DIR / "open_positions.csv"
HISTORY_FILE  = DATA_DIR / "trade_history.csv"
DAILY_FILE    = DATA_DIR / "daily_summary.csv"

# ── Column definitions ─────────────────────────────────────────────────

OPEN_COLS = [
    "trade_id", "date", "instrument", "strategy",
    "ce_symbol", "ce_strike", "ce_lots", "ce_entry_premium", "ce_entry_value",
    "pe_symbol", "pe_strike", "pe_lots", "pe_entry_premium", "pe_entry_value",
    "total_credit", "expiry", "status", "mode",
]

HISTORY_COLS = [
    "trade_id", "instrument", "strategy", "entry_date", "exit_date",
    "ce_symbol", "ce_strike", "ce_entry_premium", "ce_exit_premium",
    "pe_symbol", "pe_strike", "pe_entry_premium", "pe_exit_premium",
    "total_lots", "credit_collected", "exit_cost",
    "pnl", "pnl_pct", "exit_reason", "days_held",
]

DAILY_COLS = [
    "date", "strategy", "trades_opened", "trades_closed",
    "gross_credit", "exit_cost", "net_pnl",
    "winning_trades", "losing_trades", "win_rate",
]


# ── Internal helpers ───────────────────────────────────────────────────

def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_data_dir():
    DATA_DIR.mkdir(exist_ok=True)


def _read_csv(filepath):
    """Read a CSV into a list of dicts. Returns [] if file doesn't exist."""
    if not filepath.exists():
        return []
    with open(filepath, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(filepath, rows, cols):
    """Overwrite a CSV file with the given rows."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _append_csv(filepath, row, cols):
    """Append one row to a CSV, writing the header if the file is new."""
    file_exists = filepath.exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ── Lifecycle (no-ops — CSV needs no app lifecycle management) ─────────

def create_tracker():
    """Ensure the data directory exists. No-op if already present."""
    _ensure_data_dir()
    logger.info("CSV tracker ready — data dir: %s", DATA_DIR.resolve())


def shutdown():
    """No-op for CSV tracker — files are always flushed to disk immediately."""
    logger.info("CSV tracker shutdown (files already on disk).")


# ── Public write API ───────────────────────────────────────────────────

def add_open_position(trade):
    """Append or update a trade row in open_positions.csv."""
    _ensure_data_dir()

    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _p(leg):   return leg.entry_premium if leg else 0
    def _q(leg):   return leg.quantity      if leg else 0
    def _s(leg):   return leg.strike        if leg else ""
    def _sym(leg): return leg.symbol        if leg else ""
    def _l(leg):   return leg.lots          if leg else 0

    expiry = (ce_leg or pe_leg).expiry if (ce_leg or pe_leg) else ""

    new_row = {
        "trade_id":        trade.trade_id,
        "date":            date.today().strftime("%Y-%m-%d"),
        "instrument":      trade.instrument,
        "strategy":        trade.strategy,
        "ce_symbol":       _sym(ce_leg),
        "ce_strike":       _s(ce_leg),
        "ce_lots":         _l(ce_leg),
        "ce_entry_premium": _p(ce_leg),
        "ce_entry_value":  round(_p(ce_leg) * _q(ce_leg), 2),
        "pe_symbol":       _sym(pe_leg),
        "pe_strike":       _s(pe_leg),
        "pe_lots":         _l(pe_leg),
        "pe_entry_premium": _p(pe_leg),
        "pe_entry_value":  round(_p(pe_leg) * _q(pe_leg), 2),
        "total_credit":    round((_p(ce_leg) * _q(ce_leg)) + (_p(pe_leg) * _q(pe_leg)), 2),
        "expiry":          expiry,
        "status":          "OPEN",
        "mode":            "PAPER" if config.PAPER_TRADING else "LIVE",
    }

    # Read existing rows, replace if trade_id exists, else append
    rows = _read_csv(OPEN_FILE)
    rows = [r for r in rows if r.get("trade_id") != trade.trade_id]
    rows.append(new_row)
    _write_csv(OPEN_FILE, rows, OPEN_COLS)
    logger.info("CSV: open position added — %s", trade.trade_id)


def close_position(trade, exit_reason=""):
    """Mark trade CLOSED in open_positions.csv and append to trade_history.csv."""
    _ensure_data_dir()

    # ── Update open_positions.csv ──────────────────────────────────
    rows = _read_csv(OPEN_FILE)
    for row in rows:
        if row.get("trade_id") == trade.trade_id:
            row["status"] = "CLOSED"
    _write_csv(OPEN_FILE, rows, OPEN_COLS)

    # ── Compute P&L ────────────────────────────────────────────────
    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _v(leg, attr): return getattr(leg, attr, 0) or 0

    ce_credit    = _v(ce_leg, "entry_premium") * _v(ce_leg, "quantity")
    pe_credit    = _v(pe_leg, "entry_premium") * _v(pe_leg, "quantity")
    ce_exit      = _v(ce_leg, "exit_premium")  * _v(ce_leg, "quantity")
    pe_exit      = _v(pe_leg, "exit_premium")  * _v(pe_leg, "quantity")
    total_credit = ce_credit + pe_credit
    total_exit   = ce_exit   + pe_exit
    pnl          = total_credit - total_exit
    pnl_pct      = round(pnl / total_credit, 4) if total_credit else 0
    lots         = _v(ce_leg, "lots") + _v(pe_leg, "lots")

    entry_str = trade.entry_date.strftime("%Y-%m-%d")
    exit_str  = date.today().strftime("%Y-%m-%d")
    days_held = (date.today() - trade.entry_date).days

    hist_row = {
        "trade_id":           trade.trade_id,
        "instrument":         trade.instrument,
        "strategy":           trade.strategy,
        "entry_date":         entry_str,
        "exit_date":          exit_str,
        "ce_symbol":          _v(ce_leg, "symbol") if ce_leg else "",
        "ce_strike":          _v(ce_leg, "strike"),
        "ce_entry_premium":   _v(ce_leg, "entry_premium"),
        "ce_exit_premium":    _v(ce_leg, "exit_premium"),
        "pe_symbol":          _v(pe_leg, "symbol") if pe_leg else "",
        "pe_strike":          _v(pe_leg, "strike"),
        "pe_entry_premium":   _v(pe_leg, "entry_premium"),
        "pe_exit_premium":    _v(pe_leg, "exit_premium"),
        "total_lots":         lots,
        "credit_collected":   round(total_credit, 2),
        "exit_cost":          round(total_exit, 2),
        "pnl":                round(pnl, 2),
        "pnl_pct":            pnl_pct,
        "exit_reason":        exit_reason,
        "days_held":          days_held,
    }
    _append_csv(HISTORY_FILE, hist_row, HISTORY_COLS)
    logger.info("CSV: trade closed — %s | P&L Rs.%.0f", trade.trade_id, pnl)


def update_daily_summary(trades_opened, trades_closed,
                          gross_credit, exit_cost, winning, losing):
    """Append or update today's row in daily_summary.csv (keyed by date + strategy)."""
    _ensure_data_dir()

    today_str = date.today().strftime("%Y-%m-%d")
    strategy  = config.ACTIVE_STRATEGY
    pnl       = round(gross_credit - exit_cost, 2)
    win_rate  = round(winning / (winning + losing), 4) if (winning + losing) else 0

    new_row = {
        "date":           today_str,
        "strategy":       strategy,
        "trades_opened":  trades_opened,
        "trades_closed":  trades_closed,
        "gross_credit":   round(gross_credit, 2),
        "exit_cost":      round(exit_cost, 2),
        "net_pnl":        pnl,
        "winning_trades": winning,
        "losing_trades":  losing,
        "win_rate":       win_rate,
    }

    # Replace today's row for this strategy if it exists, else append
    rows = _read_csv(DAILY_FILE)
    rows = [r for r in rows
            if not (r.get("date") == today_str and r.get("strategy") == strategy)]
    rows.append(new_row)
    # Keep sorted by date
    rows.sort(key=lambda r: r.get("date", ""))
    _write_csv(DAILY_FILE, rows, DAILY_COLS)
    logger.info("CSV: daily summary updated for %s | Net P&L Rs.%.0f", today_str, pnl)


# ── load_open_positions — identical logic to excel_tracker.py ──────────

def load_open_positions():
    """
    Read open_positions.csv and reconstruct Trade + OptionLeg objects
    for every row where status == OPEN and mode matches current config.
    Returns list of Trade objects for self.open_trades.
    """
    from strategies import Trade, OptionLeg

    _ensure_data_dir()
    current_mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    rows   = _read_csv(OPEN_FILE)
    trades = []

    for row in rows:
        if row.get("status", "").upper() != "OPEN":
            continue

        mode = row.get("mode", "").upper()
        if mode and mode != current_mode:
            logger.info("Skipping %s: Mode=%s != current=%s",
                        row.get("trade_id"), mode, current_mode)
            continue

        def _i(k): return int(float(row[k])) if row.get(k) not in (None, "") else 0
        def _f(k): return float(row[k])      if row.get(k) not in (None, "") else 0.0

        try:
            entry_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            entry_date = date.today()

        trade = Trade(
            trade_id   = row["trade_id"],
            instrument = row.get("instrument", ""),
            strategy   = row.get("strategy", ""),
            status     = "OPEN",
            entry_date = entry_date,
        )

        ce_val     = _f("ce_entry_value")
        ce_premium = _f("ce_entry_premium")
        pe_val     = _f("pe_entry_value")
        pe_premium = _f("pe_entry_premium")
        ce_qty     = int(round(ce_val / ce_premium)) if ce_premium else _i("ce_lots")
        pe_qty     = int(round(pe_val / pe_premium)) if pe_premium else _i("pe_lots")

        if row.get("ce_symbol"):
            trade.legs.append(OptionLeg(
                symbol        = row["ce_symbol"],
                instrument    = row.get("instrument", ""),
                expiry        = row.get("expiry", ""),
                strike        = _i("ce_strike"),
                option_type   = "CE",
                lots          = _i("ce_lots"),
                quantity      = ce_qty,
                entry_premium = ce_premium,
                status        = "OPEN",
            ))

        if row.get("pe_symbol"):
            trade.legs.append(OptionLeg(
                symbol        = row["pe_symbol"],
                instrument    = row.get("instrument", ""),
                expiry        = row.get("expiry", ""),
                strike        = _i("pe_strike"),
                option_type   = "PE",
                lots          = _i("pe_lots"),
                quantity      = pe_qty,
                entry_premium = pe_premium,
                status        = "OPEN",
            ))

        trades.append(trade)
        logger.info("Resumed: %s | %s | CE %s | PE %s",
                    row["trade_id"], row.get("instrument"),
                    row.get("ce_symbol"), row.get("pe_symbol"))

    if trades:
        logger.info("Loaded %d open position(s) from CSV.", len(trades))
    else:
        logger.info("No open positions found in CSV.")

    return trades
