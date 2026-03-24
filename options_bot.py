from __future__ import annotations

"""
options_bot.py — Main entry point.
Run: python options_bot.py

Flow every scan cycle:
  1. Check market hours
  2. Fetch index spot prices
  3. Scan for entry conditions on NIFTY / BANKNIFTY
  4. Place orders if conditions met
  5. Monitor open positions for exit
  6. Update Excel tracker
"""

import logging
import os
import time
from datetime import datetime, date
from typing import List

import config
from broker import DhanBroker, get_tsl_client, generate_bool
from strategies import (
    OptionLeg, Trade, StrikeSelector, EntryFilter, ExitManager, get_strategy
)
import csv_tracker as tracker


# ── Logging setup ──────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────

def is_market_open():
    now = datetime.now().strftime("%H:%M")
    return config.MARKET_OPEN <= now <= config.MARKET_CLOSE


def compute_daily_loss(closed_trades):
    today_closed = [t for t in closed_trades if t.entry_date == date.today()]
    return sum(max(0, -t.pnl) for t in today_closed)


# ── Core logic ─────────────────────────────────────────────────────

class OptionsBot:

    def __init__(self):
        tsl          = get_tsl_client()          # authenticate once at startup
        self.broker  = DhanBroker(tsl)           # pass tsl into broker wrapper
        self.open_trades   = []                  # type: List[Trade]
        self.closed_trades = []                  # type: List[Trade]
        self.strategy = get_strategy(config.ACTIVE_STRATEGY)
        tracker.create_tracker()

        # ── Reload any OPEN positions from previous run ────────────
        resumed = tracker.load_open_positions()
        if resumed:
            self.open_trades.extend(resumed)
            logger.info("Resumed %d open position(s) from Excel tracker — "
                        "skipping new entry until these are resolved.", len(resumed))

        logger.info("OptionsBot initialised — strategy: %s | instruments: %s",
                    self.strategy.NAME, config.INSTRUMENTS)

    def _make_trade_id(self, instrument):
        ts = datetime.now().strftime("%Y%m%d%H%M")
        return "{}-{}".format(instrument[:2], ts)

    def try_entry(self, instrument):
        """Fetch market data, build context, call strategy.entry_criteria(), place orders."""

        # ── Fetch option chain (single call gives ATM, OC, expiry, lot size) ──
        atm_strike, oc = self.broker.get_option_chain(
            underlying=instrument,
            exchange="INDEX",
            expiry=config.EXPIRY_INDEX,
            num_strikes=config.NUM_STRIKES,
        )

        expiry   = self.broker.get_expiry_list(instrument, "INDEX")[config.EXPIRY_INDEX]
        lot_size = self.broker.get_lot_size_from_chain(instrument, oc)

        logger.info("[%s][%s] ATM=%d | Expiry=%s", instrument, self.strategy.NAME, atm_strike, expiry)

        # ── Risk gate (daily loss / max positions) ─────────────────
        daily_loss = compute_daily_loss(self.closed_trades)

        ef = EntryFilter(self.open_trades, daily_loss)
        combined_premium_estimate = 0   # checked inside strategy; EntryFilter checks limits
        ok, reason = ef.can_enter(instrument, config.MIN_PREMIUM)
        if not ok:
            logger.info("[%s] SKIP: %s", instrument, reason)
            return

        # ── Build context dict for strategy ───────────────────────
        context = {
            "instrument":    instrument,
            "atm_strike":    atm_strike,
            "expiry":        expiry,
            "option_chain":  oc,
            "lot_size":      lot_size,
            "open_trades":   self.open_trades,
            "closed_trades": self.closed_trades,
            "broker":        self.broker,
        }

        # ── Call strategy entry_criteria ───────────────────────────
        signal = self.strategy.entry_criteria(context)

        if signal is None:
            logger.info("[%s][%s] No entry signal this cycle", instrument, self.strategy.NAME)
            return

        # ── Place orders for each leg in the signal ────────────────
        trade = Trade(
            trade_id=self._make_trade_id(instrument),
            instrument=instrument,
            strategy=self.strategy.NAME,
        )

        for leg_def in signal.legs:
            sec_id  = leg_def["security_id"]
            symbol  = self.broker.get_security_name(sec_id)
            qty     = leg_def["lots"] * lot_size
            ltp     = leg_def.get("ltp", 0.0)
            txn     = leg_def["transaction"]   # "SELL" or "BUY"

            if txn == "SELL":
                order_id = self.broker.place_sell_order(symbol, qty)
            else:
                order_id = self.broker.place_buy_order(symbol, qty)

            time.sleep(1)

            if not order_id:
                logger.error("[%s] Order failed for %s — aborting trade", instrument, symbol)
                return

            fill = self.broker.get_executed_price(order_id, paper_ltp=ltp)

            # Resolve strike from option chain
            col_sid = "CE SECURITY_ID" if leg_def["option_type"] == "CE" else "PE SECURITY_ID"
            match   = oc[oc[col_sid] == sec_id]
            strike  = int(match["Strike Price"].iloc[0]) if not match.empty else 0

            trade.legs.append(OptionLeg(
                symbol=symbol, instrument=instrument, expiry=expiry,
                strike=strike, option_type=leg_def["option_type"],
                lots=leg_def["lots"], quantity=qty, entry_premium=fill,
                order_id=order_id,
            ))

        self.open_trades.append(trade)
        tracker.add_open_position(trade)
        logger.info("[%s][%s] Trade opened: %s | Credit Rs.%.0f",
                    instrument, self.strategy.NAME,
                    trade.trade_id, trade.total_premium_collected)

    def monitor_exits(self):
        """Call strategy.exit_criteria() and strategy.adjustment_done() per open trade."""
        for trade in list(self.open_trades):

            # Only monitor trades belonging to the active strategy
            if trade.strategy != self.strategy.NAME:
                continue

            symbols = [leg.symbol for leg in trade.legs if leg.status == "OPEN"]

            # Dhan-Tradehull returns None / failure dict instead of raising —
            # check the return value and skip this cycle if data is unavailable
            ltps = self.broker.get_ltp(symbols)
            if not ltps:
                logger.warning("[%s] LTP unavailable — skipping cycle", trade.trade_id)
                continue

            # Refresh option chain so adjustment logic has live data
            oc         = None
            atm_strike = 0
            try:
                result = self.broker.get_option_chain(
                    underlying=trade.instrument,
                    exchange="INDEX",
                    expiry=config.EXPIRY_INDEX,
                    num_strikes=config.NUM_STRIKES,
                )
                # get_option_chain returns (atm_strike, df) — check it's valid
                if result is None or result[1] is None or result[1].empty:
                    logger.warning("[%s] Option chain unavailable — skipping cycle",
                                   trade.trade_id)
                    continue
                atm_strike, oc = result
            except Exception as e:
                logger.warning("[%s] Option chain refresh failed — skipping cycle | %s",
                               trade.trade_id, e)
                continue

            # ── Build context ──────────────────────────────────────
            exit_context = {
                "trade":        trade,
                "ltps":         ltps,
                "instrument":   trade.instrument,
                "atm_strike":   atm_strike,
                "option_chain": oc,
                "broker":       self.broker,
            }

            # ── Run adjustment check first (if strategy supports it) ─
            if hasattr(self.strategy, "check_and_adjust"):
                adj_result = self.strategy.check_and_adjust(exit_context)
                # check_and_adjust returns (bool, closed_leg, new_leg)
                adjusted, closed_leg, new_leg = adj_result if isinstance(adj_result, tuple) else (adj_result, None, None)
                if adjusted:
                    tracker.update_open_position(trade)
                    if closed_leg and new_leg:
                        tracker.record_adjustment(
                            trade, closed_leg, new_leg,
                            adj_count=getattr(trade, "adj_count", 1),
                            is_straddle=getattr(trade, "adj_straddle", False),
                        )
                    logger.info("[%s][%s] Adjustment applied and recorded",
                                trade.trade_id, self.strategy.NAME)
                    continue

            # ── Call strategy exit_criteria ────────────────────────
            should_exit, reason = self.strategy.exit_criteria(exit_context)

            # Paper SL simulation — only fires when real market data is available
            # Never triggers as a substitute for failed LTP / option chain fetches
            if not should_exit and config.PAPER_TRADING and ltps and oc is not None:
                if generate_bool():
                    should_exit = True
                    reason      = "paper_sl_simulation"
                    logger.info("[PAPER] Random SL simulation triggered for %s", trade.trade_id)

            if not should_exit:
                continue

            logger.info("[%s][%s] EXIT triggered: %s | P&L Rs.%.0f",
                        trade.trade_id, self.strategy.NAME, reason, trade.pnl)

            # ── Call strategy adjustment_done ──────────────────────
            adj_context = dict(exit_context)
            adj_context["exit_reason"] = reason
            adjusted = self.strategy.adjustment_done(adj_context)

            if adjusted:
                logger.info("[%s][%s] Adjustment applied — skipping close this cycle",
                            trade.trade_id, self.strategy.NAME)
                continue

            # ── Close all open legs ────────────────────────────────
            all_closed = True
            for leg in trade.legs:
                if leg.status == "OPEN":
                    order_id = self.broker.place_buy_order(leg.symbol, leg.quantity)
                    if order_id:
                        leg.exit_premium = ltps.get(leg.symbol, leg.entry_premium)
                        leg.status = "CLOSED"
                    else:
                        all_closed = False
                        logger.error("Failed to close leg %s", leg.symbol)
                    time.sleep(1)

            if all_closed:
                trade.status = "CLOSED"
                self.open_trades.remove(trade)
                self.closed_trades.append(trade)
                tracker.close_position(trade, exit_reason=reason)

    def update_daily(self):
        today_closed  = [t for t in self.closed_trades if t.entry_date == date.today()]
        today_opened  = [t for t in self.open_trades   if t.entry_date == date.today()]

        # Only write to Daily Summary when at least one trade was closed today.
        # Open-only cycles (including resumed positions) produce no meaningful P&L row.
        if not today_closed:
            return

        gross  = sum(t.total_premium_collected for t in today_closed)
        cost   = sum(t.current_premium for t in today_closed)
        wins   = sum(1 for t in today_closed if t.pnl >= 0)
        losses = sum(1 for t in today_closed if t.pnl < 0)
        tracker.update_daily_summary(
            trades_opened=len(today_opened),
            trades_closed=len(today_closed),
            gross_credit=gross, exit_cost=cost,
            winning=wins, losing=losses,
        )

    def run(self):
        logger.info("=" * 60)
        logger.info("  Options Selling Bot STARTED")
        logger.info("  Mode        : %s", "*** PAPER TRADING ***" if config.PAPER_TRADING else "LIVE")
        logger.info("  Instruments : %s", config.INSTRUMENTS)
        logger.info("  Strategy    : %s — %s", self.strategy.NAME, self.strategy.DESCRIPTION)
        logger.info("  Profit Tgt  : %.0f%% | SL: %.1fx",
                    config.PROFIT_TARGET_PCT * 100, config.STOP_LOSS_MULTIPLIER)
        logger.info("=" * 60)

        while True:
            try:
                if not is_market_open():
                    logger.info("Market closed. Sleeping 60s...")
                    time.sleep(60)
                    continue

                for inst in config.INSTRUMENTS:
                    self.try_entry(inst)

                self.monitor_exits()
                self.update_daily()

                logger.info("Cycle complete. Open positions: %d | Sleeping %ds",
                            len(self.open_trades), config.SCAN_INTERVAL_SECONDS)
                time.sleep(config.SCAN_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break

            except Exception as e:
                logger.error("Unexpected error: %s", e, exc_info=True)
                time.sleep(30)

        # ── Clean shutdown: save workbook and close Excel app ──────
        tracker.shutdown()


if __name__ == "__main__":
    bot = OptionsBot()
    bot.run()
