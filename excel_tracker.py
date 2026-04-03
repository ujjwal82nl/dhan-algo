"""
excel_tracker.py — All Excel I/O via xlwings (xw).

Sheets:
  1. Open Positions — live trades
  2. Trade History  — closed trades
  3. Daily Summary  — per-day P&L rollup
  4. Dashboard      — key metrics (formula-driven)
  5. Adjustments    — one row per leg roll

NOTE on entry_premium semantics (strategies.py)
------------------------------------------------
  OptionLeg.entry_price   = raw per-unit fill price  (e.g. Rs. 85.50)
  OptionLeg.entry_premium = entry_price * quantity   (total Rs. for this leg)

All tracker functions read entry_premium directly as the total Rs. value and
must NOT multiply it by quantity again.
exit_premium is still a per-unit price — multiply by quantity where needed.
"""

import os
import logging
from datetime import datetime, date

import xlwings as xw

import config

logger = logging.getLogger(__name__)

# ── Module-level singletons ────────────────────────────────────────────
_APP = None   # type: xw.App
_WB  = None   # type: xw.Book

# ── Colours ────────────────────────────────────────────────────────────
NAVY      = (0x1F, 0x38, 0x64)
WHITE     = (0xFF, 0xFF, 0xFF)
GREEN_BG  = (0xE2, 0xEF, 0xDA)
RED_BG    = (0xFC, 0xE4, 0xD6)
BLUE_BG   = (0xDD, 0xEE, 0xFF)
GREY_BG   = (0xF2, 0xF2, 0xF2)
YELLOW_BG = (0xFF, 0xF2, 0xCC)
BLUE2_BG  = (0x2E, 0x50, 0x90)

# ── Column header definitions ──────────────────────────────────────────
#
# Open Positions columns (A … T  = 20 cols)
#   A  Trade ID        B  Date           C  Instrument      D  Strategy
#   E  CE Symbol       F  CE Strike      G  CE Lots
#   H  CE Entry Price  I  CE Entry Prem  (= H * qty, total Rs.)
#   J  PE Symbol       K  PE Strike      L  PE Lots
#   M  PE Entry Price  N  PE Entry Prem  (= M * qty, total Rs.)
#   O  Total Credit    P  Expiry         Q  DTE
#   R  Status          S  Mode
#
OPEN_HEADERS = [
    "Trade ID", "Date", "Instrument", "Strategy",
    "CE Symbol", "CE Strike", "CE Lots",
    "CE Entry Price", "CE Entry Prem (Rs.)",
    "PE Symbol", "PE Strike", "PE Lots",
    "PE Entry Price", "PE Entry Prem (Rs.)",
    "Total Credit (Rs.)", "Expiry", "DTE", "Status", "Mode",
]

#
# Trade History columns (A … T  = 20 cols)
#   A  Trade ID        B  Instrument     C  Strategy
#   D  Entry Date      E  Exit Date
#   F  CE Symbol       G  CE Strike
#   H  CE Entry Price  I  CE Entry Prem  J  CE Exit Rs.
#   K  PE Symbol       L  PE Strike
#   M  PE Entry Price  N  PE Entry Prem  O  PE Exit Rs.
#   P  Total Lots      Q  Credit Coll.   R  Exit Cost
#   S  P&L             T  P&L %          U  Exit Reason   V  Days Held
#
HISTORY_HEADERS = [
    "Trade ID", "Instrument", "Strategy", "Entry Date", "Exit Date",
    "CE Symbol", "CE Strike", "CE Entry Price", "CE Entry Prem (Rs.)", "CE Exit Rs.",
    "PE Symbol", "PE Strike", "PE Entry Price", "PE Entry Prem (Rs.)", "PE Exit Rs.",
    "Total Lots", "Credit Collected (Rs.)", "Exit Cost (Rs.)",
    "P&L (Rs.)", "P&L %", "Exit Reason", "Days Held",
]

DAILY_HEADERS = [
    "Date", "Trades Opened", "Trades Closed",
    "Gross Credit (Rs.)", "Total Exit Cost (Rs.)", "Net P&L (Rs.)",
    "Winning Trades", "Losing Trades", "Win Rate %",
    "Running Total P&L (Rs.)",
]

# ── Internal helpers ───────────────────────────────────────────────────

def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _rgb(r, g, b):
    return r + (g << 8) + (b << 16)

def _is_wb_alive():
    if _WB is None:
        return False
    try:
        _ = _WB.name
        return True
    except Exception:
        return False

def _ensure_open():
    global _APP, _WB
    if _is_wb_alive():
        return _WB
    logger.warning("Excel workbook was closed externally — reopening...")
    _APP = None
    _WB  = None
    create_tracker()
    return _WB

def _wb():
    if _WB is None:
        raise RuntimeError("Excel tracker not initialised — call create_tracker() first.")
    return _WB

def _get_or_add_sheet(name):
    wb = _ensure_open()
    if name not in [s.name for s in wb.sheets]:
        wb.sheets.add(name)
    return wb.sheets[name]

def _write_header_row(ws, headers, row=1):
    for col, text in enumerate(headers, 1):
        cell = ws.range((row, col))
        cell.value = text
        cell.api.Font.Bold = True
        cell.api.Font.Color = 0xFFFFFF
        cell.api.Font.Name = "Arial"
        cell.api.Font.Size = 10
        cell.api.Interior.Color = _rgb(*NAVY)
        cell.api.HorizontalAlignment = -4108
        cell.api.VerticalAlignment   = -4108
        cell.api.WrapText = True
    ws.range((row, 1), (row, len(headers))).row_height = 32

def _set_col_widths(ws, widths):
    for col, w in widths:
        ws.range((1, col)).column_width = w

def _find_next_row(ws, start_row=3):
    row = start_row
    while ws.range("A{}".format(row)).value is not None:
        row += 1
    return row

def _find_trade_row(ws, trade_id, start_row=3):
    row = start_row
    while True:
        val = ws.range("A{}".format(row)).value
        if val is None:
            return None
        if str(val) == str(trade_id):
            return row
        row += 1

def _save():
    _ensure_open().api.Save()

# ── Lifecycle ──────────────────────────────────────────────────────────

def create_tracker():
    global _APP, _WB

    if _WB is not None:
        return

    abs_path  = os.path.abspath(config.EXCEL_FILE)
    file_name = os.path.basename(abs_path)
    file_exists = os.path.exists(abs_path)

    for app in xw.apps:
        for book in app.books:
            if book.name.lower() == file_name.lower():
                _APP = app
                _WB  = book
                logger.info("Excel tracker reattached to existing open workbook: %s", file_name)
                return

    _APP = xw.App(visible=True)
    _APP.display_alerts  = False
    _APP.screen_updating = True

    if file_exists:
        _WB = _APP.books.open(abs_path)
        logger.info("Excel tracker opened: %s", abs_path)
    else:
        _WB = _APP.books.add()
        _build_all_sheets()
        _WB.save(abs_path)
        logger.info("Excel tracker created: %s", abs_path)


def shutdown():
    global _APP, _WB
    if _WB is not None:
        try:
            _WB.api.Save()
            _WB.close()
            logger.info("Excel workbook saved and closed.")
        except Exception as e:
            logger.warning("Error closing workbook: %s", e)
        _WB = None
    if _APP is not None:
        try:
            _APP.quit()
            logger.info("Excel application closed.")
        except Exception as e:
            logger.warning("Error quitting Excel app: %s", e)
        _APP = None

# ── Sheet builders ─────────────────────────────────────────────────────

def _build_all_sheets():
    wb = _wb()

    # Sheet 1: Open Positions
    ws_open = wb.sheets[0]
    ws_open.name = "Open Positions"
    ws_open.range("A1").value = "Last Updated: " + _ts()
    _write_header_row(ws_open, OPEN_HEADERS, row=2)
    _set_col_widths(ws_open, [
        (1,14),(2,12),(3,12),(4,16),
        (5,22),(6,10),(7,8),(8,14),(9,16),
        (10,22),(11,10),(12,8),(13,14),(14,16),
        (15,16),(16,12),(17,6),(18,10),(19,10),
    ])
    ws_open.range("A3").api.Select()
    _APP.api.ActiveWindow.FreezePanes = True

    # Sheet 2: Trade History
    ws_hist = wb.sheets.add("Trade History", after=ws_open)
    ws_hist.range("A1").value = "Last Updated: " + _ts()
    _write_header_row(ws_hist, HISTORY_HEADERS, row=2)
    _set_col_widths(ws_hist, [
        (1,14),(2,12),(3,16),(4,12),(5,12),
        (6,22),(7,10),(8,14),(9,16),(10,14),
        (11,22),(12,10),(13,14),(14,16),(15,14),
        (16,10),(17,18),(18,16),(19,12),(20,10),(21,20),(22,10),
    ])
    ws_hist.range("A3").api.Select()
    _APP.api.ActiveWindow.FreezePanes = True

    # Sheet 3: Daily Summary
    ws_daily = wb.sheets.add("Daily Summary", after=ws_hist)
    ws_daily.range("A1").value = "Last Updated: " + _ts()
    _write_header_row(ws_daily, DAILY_HEADERS, row=2)
    _set_col_widths(ws_daily, [
        (1,14),(2,14),(3,14),
        (4,18),(5,18),(6,14),
        (7,14),(8,14),(9,12),(10,20),
    ])
    ws_daily.range("A3").api.Select()
    _APP.api.ActiveWindow.FreezePanes = True

    # Sheet 4: Dashboard
    ws_dash = wb.sheets.add("Dashboard", after=ws_daily)
    _build_dashboard(ws_dash)


def _build_dashboard(ws):
    ws.range("A1").column_width = 32
    ws.range("B1").column_width = 22
    ws.range("C1").column_width = 4
    ws.range("D1").column_width = 32
    ws.range("E1").column_width = 22

    ws.range("A1").value = "Options Selling Dashboard"
    ws.range("A1:E1").api.Merge()
    ws.range("A1").api.Font.Bold = True
    ws.range("A1").api.Font.Size = 14
    ws.range("A1").api.Font.Color = 0xFFFFFF
    ws.range("A1").api.Interior.Color = _rgb(*NAVY)
    ws.range("A1").row_height = 28

    ws.range("A2").value = "NIFTY & BANKNIFTY | Auto-updated by options_bot.py"
    ws.range("A2:E2").api.Merge()
    ws.range("A2").api.Font.Italic = True
    ws.range("A2").api.Font.Size = 9

    # P&L Summary — P&L column is now S (col 19) in Trade History
    _dash_section(ws, "A4:B4", "P & L SUMMARY")
    _dash_kv(ws, 5,  1, "Total Realised P&L (Rs.)",  2, "B5",
             "=IFERROR(SUM('Trade History'!S3:S5000),0)",   '#,##0;(#,##0);-', True)
    _dash_kv(ws, 6,  1, "Winning Trades",             2, "B6",
             "=IFERROR(COUNTIF('Trade History'!S3:S5000,\">0\"),0)", "0", False)
    _dash_kv(ws, 7,  1, "Losing Trades",              2, "B7",
             "=IFERROR(COUNTIF('Trade History'!S3:S5000,\"<0\"),0)", "0", False)
    _dash_kv(ws, 8,  1, "Win Rate %",                 2, "B8",
             "=IFERROR(B6/(B6+B7),0)",                       "0.0%",          False)
    _dash_kv(ws, 9,  1, "Avg P&L per Trade (Rs.)",    2, "B9",
             "=IFERROR(B5/(B6+B7),0)",                       '#,##0;(#,##0);-', False)
    _dash_kv(ws, 10, 1, "Best Trade (Rs.)",            2, "B10",
             "=IFERROR(MAX('Trade History'!S3:S5000),0)",     '#,##0;(#,##0);-', False)
    _dash_kv(ws, 11, 1, "Worst Trade (Rs.)",           2, "B11",
             "=IFERROR(MIN('Trade History'!S3:S5000),0)",     '#,##0;(#,##0);-', False)

    # Open Positions — Status is now col R (18)
    _dash_section(ws, "D4:E4", "OPEN POSITIONS")
    _dash_kv(ws, 5,  4, "Open Positions",              5, "E5",
             "=IFERROR(COUNTIF('Open Positions'!R3:R500,\"OPEN\"),0)", "0", False)
    _dash_kv(ws, 6,  4, "Open Credit Collected (Rs.)", 5, "E6",
             "=IFERROR(SUMIF('Open Positions'!R3:R500,\"OPEN\",'Open Positions'!O3:O500),0)",
             '#,##0;(#,##0);-', False)

    # Instrument breakdown — P&L column is S in Trade History
    _dash_section(ws, "A13:E13", "INSTRUMENT BREAKDOWN")
    for ci, h in enumerate(["Instrument","Trades","P&L (Rs.)","Win Rate %","Avg P&L (Rs.)"], 1):
        c = ws.range((14, ci))
        c.value = h
        c.api.Font.Bold = True
        c.api.Font.Color = 0xFFFFFF
        c.api.Interior.Color = _rgb(*BLUE2_BG)
        c.api.HorizontalAlignment = -4108

    for ri, inst in [(15, "NIFTY"), (16, "BANKNIFTY")]:
        ws.range((ri, 1)).value = inst
        ws.range((ri, 1)).api.Font.Bold = True
        ws.range((ri, 2)).value = \
            "=IFERROR(COUNTIF('Trade History'!B3:B5000,\"{}\"),0)".format(inst)
        ws.range((ri, 2)).api.NumberFormat = "0"
        ws.range((ri, 3)).value = \
            "=IFERROR(SUMIF('Trade History'!B3:B5000,\"{i}\",'Trade History'!S3:S5000),0)".format(i=inst)
        ws.range((ri, 3)).api.NumberFormat = '#,##0;(#,##0);-'
        ws.range((ri, 4)).value = \
            "=IFERROR(COUNTIFS('Trade History'!B3:B5000,\"{i}\",'Trade History'!S3:S5000,\">0\")/B{r},0)".format(i=inst, r=ri)
        ws.range((ri, 4)).api.NumberFormat = "0.0%"
        ws.range((ri, 5)).value = "=IFERROR(C{r}/B{r},0)".format(r=ri)
        ws.range((ri, 5)).api.NumberFormat = '#,##0;(#,##0);-'
        for ci in range(1, 6):
            ws.range((ri, ci)).api.HorizontalAlignment = -4108


def _dash_section(ws, rng, label):
    ws.range(rng).api.Merge()
    ws.range(rng.split(":")[0]).value = label
    ws.range(rng.split(":")[0]).api.Font.Bold = True
    ws.range(rng.split(":")[0]).api.Font.Color = 0xFFFFFF
    ws.range(rng.split(":")[0]).api.Interior.Color = _rgb(*NAVY)


def _dash_kv(ws, row, label_col, label, val_col, cell_addr, formula, fmt, highlight):
    lc = ws.range((row, label_col))
    lc.value = label
    lc.api.Font.Bold = True
    lc.api.Font.Name = "Arial"
    lc.api.Font.Size = 10
    vc = ws.range(cell_addr)
    vc.value = formula
    vc.api.Font.Bold = True
    vc.api.Font.Name = "Arial"
    vc.api.Font.Size = 11
    vc.api.HorizontalAlignment = -4108
    if fmt:
        vc.api.NumberFormat = fmt
    if highlight:
        vc.color = YELLOW_BG

# ── Public write API ───────────────────────────────────────────────────

def add_open_position(trade):
    """Append a new trade row to the Open Positions sheet."""
    ws = _get_or_add_sheet("Open Positions")
    ws.range("A1").value = "Last Updated: " + _ts()

    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    expiry = _attr(ce_leg or pe_leg, "expiry", "")

    # entry_premium is already entry_price * quantity — no further multiplication.
    r = _find_next_row(ws)
    row_data = [
        trade.trade_id,
        date.today().strftime("%d-%b-%Y"),
        trade.instrument,
        trade.strategy,
        _attr(ce_leg, "symbol", ""),
        _attr(ce_leg, "strike"),
        _attr(ce_leg, "lots"),
        round(_attr(ce_leg, "entry_price"),   2),   # H: per-unit price
        round(_attr(ce_leg, "entry_premium"), 2),   # I: total Rs.
        _attr(pe_leg, "symbol", ""),
        _attr(pe_leg, "strike"),
        _attr(pe_leg, "lots"),
        round(_attr(pe_leg, "entry_price"),   2),   # M: per-unit price
        round(_attr(pe_leg, "entry_premium"), 2),   # N: total Rs.
        round(_attr(ce_leg, "entry_premium") + _attr(pe_leg, "entry_premium"), 2),  # O: total credit
        expiry,
        None,       # Q: DTE — formula below
        "OPEN",     # R
        "PAPER" if config.PAPER_TRADING else "LIVE",  # S
    ]

    ws.range("A{}".format(r)).value = row_data
    ws.range("Q{}".format(r)).value = "=IFERROR(DATEVALUE(P{})-TODAY(),\"\")".format(r)
    ws.range("Q{}".format(r)).api.NumberFormat = "0"

    ws.range("A{r}:S{r}".format(r=r)).color = BLUE_BG if r % 2 == 0 else WHITE
    for col in [1, 3, 4]:
        ws.range((r, col)).api.Font.Bold = True

    _save()
    logger.info("Excel: open position added — %s", trade.trade_id)


def update_open_position(trade):
    """
    Rewrite the Open Positions row after a leg adjustment — updates
    CE/PE symbols, strikes, entry_price and entry_premium for the rolled leg.
    """
    ws = _get_or_add_sheet("Open Positions")
    ws.range("A1").value = "Last Updated: " + _ts()

    ce_leg = next((l for l in trade.legs if l.option_type == "CE" and l.status == "OPEN"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE" and l.status == "OPEN"), None)

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    trade_row = _find_trade_row(ws, trade.trade_id)
    if not trade_row:
        logger.warning("update_open_position: trade %s not found in sheet", trade.trade_id)
        return

    expiry = _attr(ce_leg or pe_leg, "expiry", "")

    ws.range("E{}".format(trade_row)).value = _attr(ce_leg, "symbol", "")
    ws.range("F{}".format(trade_row)).value = _attr(ce_leg, "strike")
    ws.range("G{}".format(trade_row)).value = _attr(ce_leg, "lots")
    ws.range("H{}".format(trade_row)).value = round(_attr(ce_leg, "entry_price"),   2)
    ws.range("I{}".format(trade_row)).value = round(_attr(ce_leg, "entry_premium"), 2)
    ws.range("J{}".format(trade_row)).value = _attr(pe_leg, "symbol", "")
    ws.range("K{}".format(trade_row)).value = _attr(pe_leg, "strike")
    ws.range("L{}".format(trade_row)).value = _attr(pe_leg, "lots")
    ws.range("M{}".format(trade_row)).value = round(_attr(pe_leg, "entry_price"),   2)
    ws.range("N{}".format(trade_row)).value = round(_attr(pe_leg, "entry_premium"), 2)
    ws.range("O{}".format(trade_row)).value = round(
        _attr(ce_leg, "entry_premium") + _attr(pe_leg, "entry_premium"), 2
    )
    ws.range("P{}".format(trade_row)).value = expiry

    _save()
    logger.info("Excel: open position updated after adjustment — %s", trade.trade_id)


def close_position(trade, exit_reason=""):
    """Mark CLOSED in Open Positions and append a row to Trade History."""
    ws_open = _get_or_add_sheet("Open Positions")
    ws_hist = _get_or_add_sheet("Trade History")

    ws_open.range("A1").value = "Last Updated: " + _ts()
    ws_hist.range("A1").value = "Last Updated: " + _ts()

    # Mark CLOSED in Open Positions (status is col R)
    trade_row = _find_trade_row(ws_open, trade.trade_id)
    if trade_row:
        ws_open.range("R{}".format(trade_row)).value = "CLOSED"
        ws_open.range("A{r}:S{r}".format(r=trade_row)).color = GREY_BG

    ce_leg = next((l for l in trade.legs if l.option_type == "CE"), None)
    pe_leg = next((l for l in trade.legs if l.option_type == "PE"), None)

    def _attr(leg, attr, default=0):
        return getattr(leg, attr, default) if leg else default

    # entry_premium is already total Rs. — read directly.
    # exit_premium is still per-unit — multiply by quantity.
    ce_entry = _attr(ce_leg, "entry_premium")
    pe_entry = _attr(pe_leg, "entry_premium")
    ce_exit  = (_attr(ce_leg, "exit_premium") or 0) * _attr(ce_leg, "quantity")
    pe_exit  = (_attr(pe_leg, "exit_premium") or 0) * _attr(pe_leg, "quantity")

    total_credit = ce_entry + pe_entry
    total_exit   = ce_exit  + pe_exit
    pnl          = total_credit - total_exit
    pnl_pct      = pnl / total_credit if total_credit else 0
    lots         = _attr(ce_leg, "lots") + _attr(pe_leg, "lots")

    r = _find_next_row(ws_hist)
    hist_data = [
        trade.trade_id, trade.instrument, trade.strategy,
        trade.entry_date.strftime("%d-%b-%Y"), date.today().strftime("%d-%b-%Y"),
        _attr(ce_leg, "symbol", ""),
        _attr(ce_leg, "strike"),
        round(_attr(ce_leg, "entry_price"),   2),   # H: per-unit
        round(ce_entry,                       2),   # I: total Rs.
        round(_attr(ce_leg, "exit_premium") or 0, 2),  # J: per-unit exit
        _attr(pe_leg, "symbol", ""),
        _attr(pe_leg, "strike"),
        round(_attr(pe_leg, "entry_price"),   2),   # M: per-unit
        round(pe_entry,                       2),   # N: total Rs.
        round(_attr(pe_leg, "exit_premium") or 0, 2),  # O: per-unit exit
        lots,
        round(total_credit, 2),   # Q
        round(total_exit,   2),   # R
        round(pnl,          2),   # S
        round(pnl_pct,      4),   # T
        exit_reason,              # U
        None,                     # V: Days Held — formula below
    ]

    ws_hist.range("A{}".format(r)).value = hist_data
    ws_hist.range("V{}".format(r)).value = \
        "=IFERROR(DATEVALUE(E{r})-DATEVALUE(D{r}),0)".format(r=r)
    ws_hist.range("V{}".format(r)).api.NumberFormat = "0"
    ws_hist.range("S{}".format(r)).api.NumberFormat = '#,##0;(#,##0);-'
    ws_hist.range("S{}".format(r)).color = GREEN_BG if pnl >= 0 else RED_BG
    ws_hist.range("T{}".format(r)).api.NumberFormat = "0.0%"

    _save()
    logger.info("Excel: trade closed — %s | P&L Rs.%.0f", trade.trade_id, pnl)


def update_daily_summary(trades_opened, trades_closed,
                          gross_credit, exit_cost, winning, losing):
    """Append or update today's row in the Daily Summary sheet."""
    ws = _get_or_add_sheet("Daily Summary")
    today_str = date.today().strftime("%d-%b-%Y")
    ws.range("A1").value = "Last Updated: " + _ts()

    pnl      = gross_credit - exit_cost
    win_rate = winning / (winning + losing) if (winning + losing) else 0

    r = _find_trade_row(ws, today_str)
    if r is None:
        r = _find_next_row(ws)

    daily_data = [
        today_str, trades_opened, trades_closed,
        round(gross_credit, 2), round(exit_cost, 2), round(pnl, 2),
        winning, losing, round(win_rate, 4),
        "=IFERROR(SUM(F3:F{r}),F{r})".format(r=r),
    ]
    ws.range("A{}".format(r)).value = daily_data
    ws.range("I{}".format(r)).api.NumberFormat = "0.0%"
    for col in ["D", "E", "F", "J"]:
        ws.range("{}{}".format(col, r)).api.NumberFormat = '#,##0;(#,##0);-'
    ws.range("F{}".format(r)).color = GREEN_BG if pnl >= 0 else RED_BG

    _save()
    logger.info("Excel: daily summary updated for %s | Net P&L Rs.%.0f", today_str, pnl)


def update_timestamp(sheet_name="Open Positions"):
    ws = _get_or_add_sheet(sheet_name)
    ws.range("A1").value = "Last Updated: " + _ts()
    _save()


def record_adjustment(trade, closed_leg, new_leg, adj_count, is_straddle=False):
    """
    Write adjustment details to the Adjustments sheet — one row per leg roll.

    booked_pnl = entry_premium - (exit_premium * quantity)
      entry_premium is already total Rs.; exit_premium is per-unit.
    """
    ws = _get_or_add_sheet("Adjustments")

    if ws.range("A1").value is None or str(ws.range("A1").value).startswith("Last"):
        ws.range("A1").value = "Last Updated: " + _ts()
        headers = [
            "Timestamp", "Trade ID", "Instrument", "Strategy", "Adj #",
            "Rolled Side",
            "Closed Symbol", "Closed Strike",
            "Closed Entry Price", "Closed Entry Prem (Rs.)",
            "Closed Exit Rs.", "Closed Qty", "Booked P&L (Rs.)",
            "New Symbol", "New Strike",
            "New Entry Price", "New Entry Prem (Rs.)", "New Qty",
            "Is Straddle",
        ]
        for i, h in enumerate(headers, 1):
            cell = ws.range((2, i))
            cell.value = h
            cell.api.Font.Bold = True
            cell.api.Font.Color = 0xFFFFFF
            cell.api.Interior.Color = _rgb(*NAVY)
            cell.api.HorizontalAlignment = -4108
        ws.range((2, 1), (2, len(headers))).row_height = 28

    # entry_premium is already total Rs.; exit_premium is per-unit.
    exit_cost  = (closed_leg.exit_premium or 0) * closed_leg.quantity
    booked_pnl = round(closed_leg.entry_premium - exit_cost, 2)

    r = _find_next_row(ws, start_row=3)
    row_data = [
        _ts(),
        trade.trade_id, trade.instrument, trade.strategy, adj_count,
        closed_leg.option_type,
        closed_leg.symbol, closed_leg.strike,
        round(closed_leg.entry_price,   2),    # per-unit
        round(closed_leg.entry_premium, 2),    # total Rs.
        round(closed_leg.exit_premium or 0, 2),
        closed_leg.quantity,
        booked_pnl,
        new_leg.symbol, new_leg.strike,
        round(new_leg.entry_price,   2),       # per-unit
        round(new_leg.entry_premium, 2),       # total Rs.
        new_leg.quantity,
        is_straddle,
    ]

    ws.range("A{}".format(r)).value = row_data
    ws.range("M{}".format(r)).color = GREEN_BG if booked_pnl >= 0 else RED_BG
    ws.range("M{}".format(r)).api.NumberFormat = '#,##0;(#,##0);-'

    _save()
    logger.info("Excel: adjustment #%d recorded — %s | booked P&L Rs.%.0f",
                adj_count, trade.trade_id, booked_pnl)


def load_open_positions():
    """
    Read the Open Positions sheet and reconstruct Trade + OptionLeg objects
    for every OPEN row whose Mode matches the current config.

    Column layout (data rows start at row 3):
      A  Trade ID        B  Date           C  Instrument    D  Strategy
      E  CE Symbol       F  CE Strike      G  CE Lots
      H  CE Entry Price  I  CE Entry Prem  (total Rs.)
      J  PE Symbol       K  PE Strike      L  PE Lots
      M  PE Entry Price  N  PE Entry Prem  (total Rs.)
      O  Total Credit    P  Expiry         Q  DTE
      R  Status          S  Mode
    """
    from strategies import Trade, OptionLeg
    from datetime import datetime, date as date_type

    current_mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    ws     = _get_or_add_sheet("Open Positions")
    trades = []
    row    = 3

    while True:
        trade_id = ws.range("A{}".format(row)).value
        if trade_id is None:
            break

        status = str(ws.range("R{}".format(row)).value or "").strip().upper()
        mode   = str(ws.range("S{}".format(row)).value or "").strip().upper()

        if status != "OPEN":
            row += 1
            continue
        if mode and mode != current_mode:
            logger.info("Skipping row %d: Mode=%s != current=%s", row, mode, current_mode)
            row += 1
            continue

        def _int(v):   return int(v)   if v not in (None, "") else 0
        def _float(v): return float(v) if v not in (None, "") else 0.0

        instrument = ws.range("C{}".format(row)).value or ""
        strategy   = ws.range("D{}".format(row)).value or ""

        ce_symbol       = ws.range("E{}".format(row)).value or ""
        ce_strike       = _int(ws.range("F{}".format(row)).value)
        ce_lots         = _int(ws.range("G{}".format(row)).value)
        ce_entry_price  = _float(ws.range("H{}".format(row)).value)
        ce_entry_prem   = _float(ws.range("I{}".format(row)).value)

        pe_symbol       = ws.range("J{}".format(row)).value or ""
        pe_strike       = _int(ws.range("K{}".format(row)).value)
        pe_lots         = _int(ws.range("L{}".format(row)).value)
        pe_entry_price  = _float(ws.range("M{}".format(row)).value)
        pe_entry_prem   = _float(ws.range("N{}".format(row)).value)

        expiry = ws.range("P{}".format(row)).value or ""

        # Derive quantity from entry_premium / entry_price.
        # Guard against zero entry_price (e.g. legacy rows).
        ce_qty = int(round(ce_entry_prem / ce_entry_price)) if ce_entry_price else ce_lots
        pe_qty = int(round(pe_entry_prem / pe_entry_price)) if pe_entry_price else pe_lots

        # Parse entry date
        entry_date = date_type.today()
        entry_date_v = ws.range("B{}".format(row)).value
        if entry_date_v:
            try:
                if isinstance(entry_date_v, datetime):
                    entry_date = entry_date_v.date()
                else:
                    entry_date = datetime.strptime(str(entry_date_v), "%d-%b-%Y").date()
            except (ValueError, TypeError):
                pass

        trade = Trade(
            trade_id   = str(trade_id),
            instrument = str(instrument),
            strategy   = str(strategy),
            status     = "OPEN",
            entry_date = entry_date,
        )

        if ce_symbol:
            trade.legs.append(OptionLeg(
                symbol        = str(ce_symbol),
                instrument    = str(instrument),
                expiry        = str(expiry),
                strike        = ce_strike,
                option_type   = "CE",
                lots          = ce_lots,
                quantity      = ce_qty,
                entry_price   = ce_entry_price,
                entry_premium = ce_entry_prem,
                status        = "OPEN",
            ))

        if pe_symbol:
            trade.legs.append(OptionLeg(
                symbol        = str(pe_symbol),
                instrument    = str(instrument),
                expiry        = str(expiry),
                strike        = pe_strike,
                option_type   = "PE",
                lots          = pe_lots,
                quantity      = pe_qty,
                entry_price   = pe_entry_price,
                entry_premium = pe_entry_prem,
                status        = "OPEN",
            ))

        trades.append(trade)
        logger.info("Resumed: %s | %s | CE %s | PE %s",
                    trade_id, instrument, ce_symbol, pe_symbol)
        row += 1

    if trades:
        logger.info("Loaded %d open position(s) from Excel tracker.", len(trades))
    else:
        logger.info("No open positions found in Excel tracker.")

    return trades