from __future__ import annotations
from tabulate import tabulate

"""
options_bot.py — Main entry point.

Run: python options_bot.py

Flow every scan cycle:
  1. Check market hours
  2. For each instrument, look up its exchange from config.INSTRUMENTS
  3. Scan for entry conditions
  4. Place orders if conditions met (exchange passed to all broker calls)
  5. Monitor open positions for exit (exchange from Trade object)
  6. Update CSV tracker
"""

import logging
import os
import time
from datetime import datetime, date
from typing import List

import config
from broker import DhanBroker, get_tsl_client, generate_bool
from strategies import (
    OptionLeg, Trade, EntryFilter, ExitManager, get_strategy
)
import csv_tracker as tracker

# ── Logging ────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────

def is_market_open():
    now = datetime.now().strftime("%H:%M")
    return config.MARKET_OPEN <= now <= config.MARKET_CLOSE


def compute_daily_loss(closed_trades):
    today = date.today()
    return sum(max(0, -t.pnl) for t in closed_trades if t.entry_date == today)


# ── Core bot ───────────────────────────────────────────────────────────

class OptionsBot:

    def __init__(self):
        tsl = get_tsl_client()
        self.broker        = DhanBroker(tsl)
        self.open_trades:   List[Trade] = []
        self.closed_trades: List[Trade] = []
        self.strategy = get_strategy(config.ACTIVE_STRATEGY)

        tracker.create_tracker()

        resumed = tracker.load_open_positions()
        if resumed:
            self.open_trades.extend(resumed)
            logger.info(
                "Resumed %d open position(s) from CSV tracker — "
                "skipping new entry until these are resolved.", len(resumed),
            )

        logger.info(
            "OptionsBot initialised — strategy: %s | instruments: %s",
            self.strategy.NAME,
            list(config.INSTRUMENTS.keys()),
        )

    def show_open_positions(self, trade, ltp_map):
        """
        Formats and logs open legs using already fetched LTP data.
        """
        # 1. Extract only the legs that are currently OPEN
        open_legs = [leg for leg in trade.legs if leg.status == "OPEN"]
        
        if not open_legs:
            return

        # 2. Build the table rows using the provided ltp_map
        table_data = []
        for leg in open_legs:
            # Use .get() to avoid crashes if a symbol is missing from the map
            ltp = ltp_map.get(leg.symbol, 0.0)
            #pnl = (leg.entry_price - ltp) * leg.quantity if leg.txn == "SELL" else (ltp - leg.entry_price) * leg.quantity
            pnl = (leg.entry_price - ltp) * leg.quantity
            
            table_data.append([
                leg.symbol, 
                leg.quantity, 
                f"{leg.entry_price:.2f}", 
                f"{ltp:.2f}",
                f"{pnl:.2f}"
            ])

        # 3. Create the table string
        table_output = tabulate(
            table_data, 
            headers=["SYMBOL", "QTY", "ENTRY", "LTP", "PNL"], 
            tablefmt="pipe",
            numalign="right"
        )

        # 4. Log the output
        logger.info("[%s][%s] Strategy Snapshot:\n%s",
                        trade.trade_id, 
                        self.strategy.NAME, 
                        table_output)

    def _make_trade_id(self, instrument):
        """Two-letter prefix from instrument + timestamp."""
        ts = datetime.now().strftime("%Y%m%d%H%M")
        return "{}-{}".format(instrument[:2].upper(), ts)

    # ── Entry ──────────────────────────────────────────────────────────

    def try_entry(self, instrument, exchange):
        """
        Fetch market data for *instrument* on *exchange*, build context,
        call strategy.entry_criteria(), place orders.
        exchange is always taken from config.INSTRUMENTS — never hardcoded.
        """
        # Option chain: exchange string comes from config, not hardcoded
        atm_strike, oc = self.broker.get_option_chain(
            underlying  = instrument,
            exchange    = exchange,
            expiry      = config.EXPIRY_INDEX,
            num_strikes = config.NUM_STRIKES,
        )
        expiry   = self.broker.get_expiry_list(instrument, exchange)[config.EXPIRY_INDEX]
        lot_size = self.broker.get_lot_size_from_chain(instrument, oc)

        logger.info("[%s/%s][%s] ATM=%s | Expiry=%s",
                    instrument, exchange, self.strategy.NAME, atm_strike, expiry)

        daily_loss = compute_daily_loss(self.closed_trades)
        ef = EntryFilter(self.open_trades, daily_loss)
        ok, reason = ef.can_enter(instrument, config.MIN_PREMIUM)
        if not ok:
            logger.info("[%s] SKIP: %s", instrument, reason)
            return

        context = {
            "instrument":    instrument,
            "exchange":      exchange,        # ← now in context for strategies
            "atm_strike":    atm_strike,
            "expiry":        expiry,
            "option_chain":  oc,
            "lot_size":      lot_size,
            "open_trades":   self.open_trades,
            "closed_trades": self.closed_trades,
            "broker":        self.broker,
        }

        signal = self.strategy.entry_criteria(context)
        if signal is None:
            logger.info("[%s][%s] No entry signal this cycle",
                        instrument, self.strategy.NAME)
            return

        trade = Trade(
            trade_id   = self._make_trade_id(instrument),
            instrument = instrument,
            exchange   = exchange,            # ← stored on Trade
            strategy   = self.strategy.NAME,
        )

        for leg_def in signal.legs:
            sec_id = leg_def["security_id"]
            symbol = self.broker.get_security_name(sec_id)
            qty    = leg_def["lots"] * lot_size
            ltp    = leg_def.get("ltp", 0.0)
            txn    = leg_def["transaction"]

            # Order placement: exchange for F&O legs is the instrument's own exchange.
            # INDEX options trade on NFO; MCX options trade on MCX.
            order_exchange = "NFO" if exchange == "INDEX" else exchange
            if txn == "SELL":
                order_id = self.broker.place_sell_order(symbol, qty,
                                                        exchange=order_exchange)
            else:
                order_id = self.broker.place_buy_order(symbol, qty,
                                                       exchange=order_exchange)
            time.sleep(1)

            if not order_id:
                logger.error("[%s] Order failed for %s — aborting trade",
                             instrument, symbol)
                return

            fill = self.broker.get_executed_price(order_id, paper_ltp=ltp)

            col_sid = "CE SECURITY_ID" if leg_def["option_type"] == "CE" \
                      else "PE SECURITY_ID"
            match  = oc[oc[col_sid] == sec_id]
            strike = int(match["Strike Price"].iloc[0]) if not match.empty else 0

            trade.legs.append(OptionLeg(
                symbol        = symbol,
                instrument    = instrument,
                exchange      = exchange,     # ← stored on OptionLeg
                expiry        = expiry,
                strike        = strike,
                option_type   = leg_def["option_type"],
                lots          = leg_def["lots"],
                quantity      = qty,
                entry_price   = fill,
                entry_premium = fill * qty,
                order_id      = order_id,
            ))

        self.open_trades.append(trade)
        tracker.add_open_position(trade)
        logger.info("[%s/%s][%s] Trade opened: %s | Credit Rs.%.0f",
                    instrument, exchange, self.strategy.NAME,
                    trade.trade_id, trade.total_premium_collected)

    # ── Exit monitoring ────────────────────────────────────────────────

    def monitor_exits(self):
        """Call strategy exit/adjustment logic per open trade."""

        for trade in list(self.open_trades):
            if trade.strategy != self.strategy.NAME:
                continue

            symbols = [leg.symbol for leg in trade.legs if leg.status == "OPEN"]
            ltps    = self.broker.get_ltp(symbols)
            if not ltps:
                logger.warning("[%s] LTP unavailable — skipping cycle", trade.trade_id)
                continue

            # Option chain refresh: use exchange stored on the Trade object
            oc         = None
            atm_strike = 0
            try:
                result = self.broker.get_option_chain(
                    underlying  = trade.instrument,
                    exchange    = trade.exchange,   # ← from Trade, not hardcoded
                    expiry      = config.EXPIRY_INDEX,
                    num_strikes = config.NUM_STRIKES,
                )
                if result is None or result[1] is None or result[1].empty:
                    logger.warning("[%s] Option chain unavailable — skipping cycle",
                                   trade.trade_id)
                    continue
                atm_strike, oc = result
            except Exception as e:
                logger.warning("[%s] Option chain refresh failed — skipping | %s",
                               trade.trade_id, e)
                continue

            # Show open legs with current LTPs for better visibility in logs
            self.show_open_positions(trade, ltps)

            exit_context = {
                "trade":        trade,
                "ltps":         ltps,
                "instrument":   trade.instrument,
                "exchange":     trade.exchange,     # ← available to strategies
                "atm_strike":   atm_strike,
                "option_chain": oc,
                "broker":       self.broker,
            }

            # Adjustment check before exit check
            if hasattr(self.strategy, "check_and_adjust"):
                adj_result = self.strategy.check_and_adjust(exit_context)
                adjusted, closed_leg, new_leg = (
                    adj_result if isinstance(adj_result, tuple)
                    else (adj_result, None, None)
                )
                if adjusted:
                    tracker.update_open_position(trade)
                    if closed_leg and new_leg:
                        tracker.record_adjustment(
                            trade, closed_leg, new_leg,
                            adj_count   = getattr(trade, "adj_count", 1),
                            is_straddle = getattr(trade, "adj_straddle", False),
                        )
                    logger.info("[%s][%s] Adjustment applied and recorded",
                                trade.trade_id, self.strategy.NAME)
                    continue

            should_exit, reason = self.strategy.exit_criteria(exit_context)

            # Ujjwal: commented out for not closing the positions.
            # if not should_exit and config.PAPER_TRADING and ltps and oc is not None:
            #     if generate_bool():
            #         should_exit = True
            #         reason      = "paper_sl_simulation"
            #         logger.info("[PAPER] Random SL simulation triggered for %s",
            #                     trade.trade_id)

            if not should_exit:
                continue

            logger.info("[%s][%s] EXIT triggered: %s | P&L Rs.%.0f",
                        trade.trade_id, self.strategy.NAME, reason, trade.pnl)

            adj_context = dict(exit_context)
            adj_context["exit_reason"] = reason
            if self.strategy.adjustment_done(adj_context):
                logger.info("[%s][%s] Adjustment applied — skipping close this cycle",
                            trade.trade_id, self.strategy.NAME)
                continue

            # Close all open legs
            order_exchange = "NFO" if trade.exchange == "INDEX" else trade.exchange
            all_closed = True
            for leg in trade.legs:
                if leg.status == "OPEN":
                    order_id = self.broker.place_buy_order(
                        leg.symbol, leg.quantity, exchange=order_exchange
                    )
                    if order_id:
                        leg.exit_premium = ltps.get(leg.symbol, leg.entry_price)
                        leg.status       = "CLOSED"
                    else:
                        all_closed = False
                        logger.error("Failed to close leg %s", leg.symbol)
                    time.sleep(1)

            if all_closed:
                trade.status = "CLOSED"
                self.open_trades.remove(trade)
                self.closed_trades.append(trade)
                tracker.close_position(trade, exit_reason=reason)

    # ── Daily summary ──────────────────────────────────────────────────

    def update_daily(self):
        today_closed = [t for t in self.closed_trades if t.entry_date == date.today()]
        today_opened = [t for t in self.open_trades   if t.entry_date == date.today()]
        if not today_closed:
            return
        gross  = sum(t.total_premium_collected for t in today_closed)
        cost   = sum(t.current_premium         for t in today_closed)
        wins   = sum(1 for t in today_closed if t.pnl >= 0)
        losses = sum(1 for t in today_closed if t.pnl < 0)
        tracker.update_daily_summary(
            trades_opened = len(today_opened),
            trades_closed = len(today_closed),
            gross_credit  = gross,
            exit_cost     = cost,
            winning       = wins,
            losing        = losses,
        )

    # ── Main loop ──────────────────────────────────────────────────────

    def run(self):
        logger.info("=" * 60)
        logger.info(" Options Selling Bot STARTED")
        logger.info(" Mode        : %s",
                    "*** PAPER TRADING ***" if config.PAPER_TRADING else "LIVE")
        logger.info(" Instruments : %s", config.INSTRUMENTS)
        logger.info(" Strategy    : %s — %s",
                    self.strategy.NAME, self.strategy.DESCRIPTION)
        logger.info(" Profit Tgt  : %.0f%% | SL: %.1fx",
                    config.PROFIT_TARGET_PCT * 100, config.STOP_LOSS_MULTIPLIER)
        logger.info("=" * 60)

        while True:
            try:
                if not is_market_open():
                    logger.info("Market closed. Sleeping 60s...")
                    time.sleep(60)
                    continue

                # Iterate over the dict — gives (instrument, exchange) pairs
                for instrument, exchange in config.INSTRUMENTS.items():
                    self.try_entry(instrument, exchange)

                self.monitor_exits()
                self.update_daily()

                logger.info("Cycle complete. Open positions: %d | Sleeping %ds",
                            len(self.open_trades), config.SCAN_INTERVAL_SECONDS)
                time.sleep(config.SCAN_INTERVAL_SECONDS)
                logger.info("=" * 60)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except Exception as e:
                logger.error("Unexpected error: %s", e, exc_info=True)
                time.sleep(30)

        tracker.shutdown()


if __name__ == "__main__":
    bot = OptionsBot()
    bot.run()