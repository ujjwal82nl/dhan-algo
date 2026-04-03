from __future__ import annotations

"""
csv_tracker.py — Persistent trade log via lightweight CSV files.

Files written:
  data/open_positions.csv   — all positions (OPEN and CLOSED), one row per trade
  data/trade_history.csv    — append-only, one row per closed trade
  data/daily_summary.csv    — append-only, one row per day per strategy
  data/adjustments.csv      — append-only, one row per leg roll

NOTE on premium semantics
-------------------------
  OptionLeg.entry_price   = raw per-unit fill price
  OptionLeg.entry_premium = entry_price * quantity  (total Rs.)
  OptionLeg.exit_premium  = per-unit exit price  (multiply by qty for exit cost)
"""

import os
import csv
import logging
from datetime import datetime, date
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DATA_DIR     = Path("data")
OPEN_FILE    = DATA_DIR / "open_positions.csv"
HISTORY_FILE = DATA_DIR / "trade_history.csv"
DAILY_FILE   = DATA_DIR / "daily_summary.csv"
ADJUST_FILE  = DATA_DIR / "adjustments.csv"

# ── Column definitions ─────────────────────────────────────────────────

OPEN_COLS = [
    "trade_id", "date", "instrument", "exchange", "strategy",
    "ce_symbol", "ce_strike", "ce_lots", "ce_entry_price", "ce_entry_premium",
    "pe_symbol", "pe_strike", "pe_lots", "pe_entry_price", "pe_entry_premium",
    "total_credit", "expiry", "status", "mode",
]

HISTORY_COLS = [
    "trade_id", "instrument", "exchange", "strategy", "entry_date", "exit_date",
    "ce_symbol", "ce_strike", "ce_entry_price", "ce_entry_premium", "ce_exit_premium",
    "pe_symbol", "pe_strike", "pe_entry_price", "pe_entry_premium", "pe_exit_premium",
    "total_lots", "credit_collected", "exit_cost",
    "pnl", "pnl_pct", "exit_reason", "days_held",
]

DAILY_COLS = [
    "date", "strategy", "trades_opened", "trades_closed",
    "gross_credit", "exit_cost", "net_pnl",
    "winning_trades", "losing_trades", "win_rate",
]

ADJUST_COLS = [
    "timestamp", "trade_id", "instrument", "exchange", "strategy", "adj_number",
    "rolled_side",
    "closed_symbol", "closed_strike", "closed_entry_price", "closed_entry_premium",
    "closed_exit_premium", "closed_qty", "booked_pnl",
    "new_symbol", "new_strike", "new_entry_price", "new_entry_premium", "new_qty",
    "is_straddle",
]

# ── Internal helpers ───────────────────────────────────────────────────

def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ensure_data_dir():
    DATA_DIR.mkdir(exist_ok=True)

def _read_csv(filepath):
    if not filepath.exists():
        return []
    with open(filepath, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def _write_csv(filepath, rows, cols):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def _append_csv(filepath, row, cols):
    file_exists = filepath.exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

# ── Lifecycle ──────────────────────────────────────────────────────────

def create_tracker():
    _ensure_data_dir()
    logger.info("CSV tracker ready — data dir: %s", DATA_DIR.resolve())

def shutdown():
    logger.info("CSV tracker shutdown (files already on disk).")

# ── Open positions ─────────────────────────────────────────────────────

def add_open_position(trade):
    """Write a new row to open_positions.csv for a freshly opened trade."""
    _ensure_data_dir()

    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    expiry = _attr(ce_leg or pe_leg, "expiry", "")

    new_row = {
        "trade_id":         trade.trade_id,
        "date":             date.today().strftime("%Y-%m-%d"),
        "instrument":       trade.instrument,
        "exchange":         trade.exchange,          # ← new column
        "strategy":         trade.strategy,
        "ce_symbol":        _attr(ce_leg, "symbol", ""),
        "ce_strike":        _attr(ce_leg, "strike"),
        "ce_lots":          _attr(ce_leg, "lots"),
        "ce_entry_price":   round(_attr(ce_leg, "entry_price"),   2),
        "ce_entry_premium": round(_attr(ce_leg, "entry_premium"), 2),
        "pe_symbol":        _attr(pe_leg, "symbol", ""),
        "pe_strike":        _attr(pe_leg, "strike"),
        "pe_lots":          _attr(pe_leg, "lots"),
        "pe_entry_price":   round(_attr(pe_leg, "entry_price"),   2),
        "pe_entry_premium": round(_attr(pe_leg, "entry_premium"), 2),
        "total_credit":     round(
            _attr(ce_leg, "entry_premium") + _attr(pe_leg, "entry_premium"), 2
        ),
        "expiry":  expiry,
        "status":  "OPEN",
        "mode":    "PAPER" if config.PAPER_TRADING else "LIVE",
    }

    rows = _read_csv(OPEN_FILE)
    rows = [r for r in rows if r.get("trade_id") != trade.trade_id]
    rows.append(new_row)
    _write_csv(OPEN_FILE, rows, OPEN_COLS)
    logger.info("CSV: open position added — %s", trade.trade_id)


def update_open_position(trade):
    """Rewrite the open_positions.csv row after a leg adjustment."""
    _ensure_data_dir()

    ce_leg = next(
        (l for l in trade.legs if l.option_type == "CE" and l.status == "OPEN"), None
    )
    pe_leg = next(
        (l for l in trade.legs if l.option_type == "PE" and l.status == "OPEN"), None
    )

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    expiry = _attr(ce_leg or pe_leg, "expiry", "")

    rows = _read_csv(OPEN_FILE)
    for row in rows:
        if row.get("trade_id") == trade.trade_id:
            row["exchange"]         = trade.exchange
            row["ce_symbol"]        = _attr(ce_leg, "symbol", "")
            row["ce_strike"]        = _attr(ce_leg, "strike")
            row["ce_lots"]          = _attr(ce_leg, "lots")
            row["ce_entry_price"]   = round(_attr(ce_leg, "entry_price"),   2)
            row["ce_entry_premium"] = round(_attr(ce_leg, "entry_premium"), 2)
            row["pe_symbol"]        = _attr(pe_leg, "symbol", "")
            row["pe_strike"]        = _attr(pe_leg, "strike")
            row["pe_lots"]          = _attr(pe_leg, "lots")
            row["pe_entry_price"]   = round(_attr(pe_leg, "entry_price"),   2)
            row["pe_entry_premium"] = round(_attr(pe_leg, "entry_premium"), 2)
            row["total_credit"]     = round(
                _attr(ce_leg, "entry_premium") + _attr(pe_leg, "entry_premium"), 2
            )
            row["expiry"] = expiry
            break

    _write_csv(OPEN_FILE, rows, OPEN_COLS)
    logger.info("CSV: open position updated after adjustment — %s", trade.trade_id)


def close_position(trade, exit_reason=""):
    """Mark CLOSED in open_positions.csv and append to trade_history.csv."""
    _ensure_data_dir()

    rows = _read_csv(OPEN_FILE)
    for row in rows:
        if row.get("trade_id") == trade.trade_id:
            row["status"] = "CLOSED"
    _write_csv(OPEN_FILE, rows, OPEN_COLS)

    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    ce_entry = _attr(ce_leg, "entry_premium")
    pe_entry = _attr(pe_leg, "entry_premium")
    ce_exit  = (_attr(ce_leg, "exit_premium") or 0) * _attr(ce_leg, "quantity")
    pe_exit  = (_attr(pe_leg, "exit_premium") or 0) * _attr(pe_leg, "quantity")

    total_credit = ce_entry + pe_entry
    total_exit   = ce_exit  + pe_exit
    pnl          = total_credit - total_exit
    pnl_pct      = round(pnl / total_credit, 4) if total_credit else 0
    days_held    = (date.today() - trade.entry_date).days

    hist_row = {
        "trade_id":          trade.trade_id,
        "instrument":        trade.instrument,
        "exchange":          trade.exchange,          # ← new column
        "strategy":          trade.strategy,
        "entry_date":        trade.entry_date.strftime("%Y-%m-%d"),
        "exit_date":         date.today().strftime("%Y-%m-%d"),
        "ce_symbol":         _attr(ce_leg, "symbol", ""),
        "ce_strike":         _attr(ce_leg, "strike"),
        "ce_entry_price":    round(_attr(ce_leg, "entry_price"),   2),
        "ce_entry_premium":  round(ce_entry, 2),
        "ce_exit_premium":   round(_attr(ce_leg, "exit_premium") or 0, 2),
        "pe_symbol":         _attr(pe_leg, "symbol", ""),
        "pe_strike":         _attr(pe_leg, "strike"),
        "pe_entry_price":    round(_attr(pe_leg, "entry_price"),   2),
        "pe_entry_premium":  round(pe_entry, 2),
        "pe_exit_premium":   round(_attr(pe_leg, "exit_premium") or 0, 2),
        "total_lots":        _attr(ce_leg, "lots") + _attr(pe_leg, "lots"),
        "credit_collected":  round(total_credit, 2),
        "exit_cost":         round(total_exit,   2),
        "pnl":               round(pnl,          2),
        "pnl_pct":           pnl_pct,
        "exit_reason":       exit_reason,
        "days_held":         days_held,
    }

    _append_csv(HISTORY_FILE, hist_row, HISTORY_COLS)
    logger.info("CSV: trade closed — %s | P&L Rs.%.0f", trade.trade_id, pnl)


# ── Adjustments ────────────────────────────────────────────────────────

def record_adjustment(trade, closed_leg, new_leg, adj_count, is_straddle=False):
    """Append one row to adjustments.csv for a leg roll."""
    _ensure_data_dir()

    exit_cost  = (closed_leg.exit_premium or 0) * closed_leg.quantity
    booked_pnl = round(closed_leg.entry_premium - exit_cost, 2)

    row = {
        "timestamp":            _ts(),
        "trade_id":             trade.trade_id,
        "instrument":           trade.instrument,
        "exchange":             trade.exchange,        # ← new column
        "strategy":             trade.strategy,
        "adj_number":           adj_count,
        "rolled_side":          closed_leg.option_type,
        "closed_symbol":        closed_leg.symbol,
        "closed_strike":        closed_leg.strike,
        "closed_entry_price":   round(closed_leg.entry_price,   2),
        "closed_entry_premium": round(closed_leg.entry_premium, 2),
        "closed_exit_premium":  round(closed_leg.exit_premium or 0, 2),
        "closed_qty":           closed_leg.quantity,
        "booked_pnl":           booked_pnl,
        "new_symbol":           new_leg.symbol,
        "new_strike":           new_leg.strike,
        "new_entry_price":      round(new_leg.entry_price,   2),
        "new_entry_premium":    round(new_leg.entry_premium, 2),
        "new_qty":              new_leg.quantity,
        "is_straddle":          is_straddle,
    }

    _append_csv(ADJUST_FILE, row, ADJUST_COLS)
    logger.info(
        "CSV: adjustment #%d recorded — %s | rolled %s | booked P&L Rs.%.0f",
        adj_count, trade.trade_id, closed_leg.option_type, booked_pnl,
    )


# ── Daily summary ──────────────────────────────────────────────────────

def update_daily_summary(trades_opened, trades_closed,
                          gross_credit, exit_cost, winning, losing):
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
        "exit_cost":      round(exit_cost,    2),
        "net_pnl":        pnl,
        "winning_trades": winning,
        "losing_trades":  losing,
        "win_rate":       win_rate,
    }

    rows = _read_csv(DAILY_FILE)
    rows = [r for r in rows
            if not (r.get("date") == today_str and r.get("strategy") == strategy)]
    rows.append(new_row)
    rows.sort(key=lambda r: r.get("date", ""))
    _write_csv(DAILY_FILE, rows, DAILY_COLS)
    logger.info("CSV: daily summary updated for %s | Net P&L Rs.%.0f", today_str, pnl)


# ── Resume open positions ──────────────────────────────────────────────

def load_open_positions():
    """
    Reconstruct Trade + OptionLeg objects from open_positions.csv.

    The exchange column is read from the CSV. If it is missing (legacy rows
    written before this change), it falls back to config.INSTRUMENTS lookup,
    and finally to "INDEX" as a last resort so old files don't crash.
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
        def _f(k): return float(row[k])       if row.get(k) not in (None, "") else 0.0

        instrument = row.get("instrument", "")

        # Exchange: prefer CSV column, fall back to config, then default to INDEX
        exchange = (
            row.get("exchange")
            or config.INSTRUMENTS.get(instrument)
            or "INDEX"
        )

        try:
            entry_date = datetime.strptime(row.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            entry_date = date.today()

        trade = Trade(
            trade_id   = row["trade_id"],
            instrument = instrument,
            exchange   = exchange,
            strategy   = row.get("strategy", ""),
            status     = "OPEN",
            entry_date = entry_date,
        )

        ce_entry_price = _f("ce_entry_price")
        ce_entry_prem  = _f("ce_entry_premium")
        pe_entry_price = _f("pe_entry_price")
        pe_entry_prem  = _f("pe_entry_premium")

        ce_qty = int(round(ce_entry_prem / ce_entry_price)) if ce_entry_price else _i("ce_lots")
        pe_qty = int(round(pe_entry_prem / pe_entry_price)) if pe_entry_price else _i("pe_lots")

        if row.get("ce_symbol"):
            trade.legs.append(OptionLeg(
                symbol        = row["ce_symbol"],
                instrument    = instrument,
                exchange      = exchange,
                expiry        = row.get("expiry", ""),
                strike        = _i("ce_strike"),
                option_type   = "CE",
                lots          = _i("ce_lots"),
                quantity      = ce_qty,
                entry_price   = ce_entry_price,
                entry_premium = ce_entry_prem,
                status        = "OPEN",
            ))

        if row.get("pe_symbol"):
            trade.legs.append(OptionLeg(
                symbol        = row["pe_symbol"],
                instrument    = instrument,
                exchange      = exchange,
                expiry        = row.get("expiry", ""),
                strike        = _i("pe_strike"),
                option_type   = "PE",
                lots          = _i("pe_lots"),
                quantity      = pe_qty,
                entry_price   = pe_entry_price,
                entry_premium = pe_entry_prem,
                status        = "OPEN",
            ))

        trades.append(trade)
        logger.info("Resumed: %s | %s/%s | CE %s | PE %s",
                    row["trade_id"], instrument, exchange,
                    row.get("ce_symbol"), row.get("pe_symbol"))

    if trades:
        logger.info("Loaded %d open position(s) from CSV.", len(trades))
    else:
        logger.info("No open positions found in CSV.")

    return trades