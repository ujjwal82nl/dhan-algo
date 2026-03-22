from __future__ import annotations

"""
strategy_shortStrangle.py — Strategy: "shortStrangle"

This is your first custom strategy plugin.
Fill in entry_criteria(), exit_criteria(), and adjustment_done()
with your own logic. The rest of the bot is untouched.

To activate:  set ACTIVE_STRATEGY = "shortStrangle" in config.py
To deactivate: switch ACTIVE_STRATEGY to a different strategy name.
"""

import logging
from strategy_base import BaseStrategy, EntrySignal
import config

logger = logging.getLogger(__name__)


class ShortStrangleStrategy(BaseStrategy):

    NAME        = "shortStrangle"
    DESCRIPTION = "Delta-based short strangle without adjustments"

    # ──────────────────────────────────────────────────────────────
    # ENTRY CRITERIA
    # Called once per instrument per scan cycle.
    #
    # context keys available:
    #   instrument, atm_strike, expiry, option_chain (DataFrame),
    #   lot_size, open_trades, closed_trades, broker
    #
    # Return None to skip, or EntrySignal(...) to enter.
    # ──────────────────────────────────────────────────────────────
    def entry_criteria(self, context):

        instrument   = context["instrument"]
        atm_strike   = context["atm_strike"]
        oc           = context["option_chain"]
        lot_size     = context["lot_size"]
        open_trades  = context["open_trades"]

        # ── Guard: skip if already in this instrument ──────────────
        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in open_trades
        )
        if already_in:
            logger.info("[%s][shortStrangle] Already in position — skip entry", instrument)
            return None

        # ── YOUR ENTRY LOGIC GOES HERE ─────────────────────────────
        # Example: sell CE and PE near TARGET_DELTA (short strangle)
        #
        # Step 1: filter option chain for desired delta range
        ce_candidates = oc[
            (oc["CE Delta"] >= config.TARGET_DELTA - 0.05) &
            (oc["CE Delta"] <= config.TARGET_DELTA + 0.05)
        ]
        pe_candidates = oc[
            (oc["PE Delta"].abs() >= config.TARGET_DELTA - 0.05) &
            (oc["PE Delta"].abs() <= config.TARGET_DELTA + 0.05)
        ]

        if ce_candidates.empty or pe_candidates.empty:
            logger.info("[%s][shortStrangle] No strikes in delta range — skip entry", instrument)
            return None

        # Step 2: pick highest OI strike in each side
        ce_row = ce_candidates.sort_values("CE OI").iloc[-1]
        pe_row = pe_candidates.sort_values("PE OI").iloc[-1]

        ce_ltp = float(ce_row["CE LTP"])
        pe_ltp = float(pe_row["PE LTP"])

        # Step 3: minimum premium check
        if (ce_ltp + pe_ltp) < config.MIN_PREMIUM:
            logger.info("[%s][shortStrangle] Combined premium Rs.%.1f below MIN — skip",
                        instrument, ce_ltp + pe_ltp)
            return None

        # Step 4: build and return EntrySignal
        logger.info("[%s][shortStrangle] Entry signal: CE %d @ Rs.%.1f | PE %d @ Rs.%.1f",
                    instrument,
                    int(ce_row["Strike Price"]), ce_ltp,
                    int(pe_row["Strike Price"]), pe_ltp)

        return EntrySignal(
            strategy_name=self.NAME,
            legs=[
                {
                    "security_id":  int(ce_row["CE SECURITY_ID"]),
                    "option_type":  "CE",
                    "transaction":  "SELL",
                    "ltp":          ce_ltp,
                    "lots":         config.MAX_LOTS_PER_TRADE,
                },
                {
                    "security_id":  int(pe_row["PE SECURITY_ID"]),
                    "option_type":  "PE",
                    "transaction":  "SELL",
                    "ltp":          pe_ltp,
                    "lots":         config.MAX_LOTS_PER_TRADE,
                },
            ],
        )

    # ──────────────────────────────────────────────────────────────
    # EXIT CRITERIA
    # Called once per open trade per scan cycle.
    #
    # context keys available:
    #   trade (Trade object), ltps ({symbol: ltp}),
    #   instrument, option_chain, broker
    #
    # Return (False, "") to hold, or (True, "reason") to close.
    # ──────────────────────────────────────────────────────────────
    def exit_criteria(self, context):

        trade = context["trade"]
        ltps  = context["ltps"]

        # Update live prices on legs
        for leg in trade.legs:
            if leg.symbol in ltps:
                leg.exit_premium = ltps[leg.symbol]

        collected   = trade.total_premium_collected
        current_val = trade.current_premium
        pnl         = collected - current_val

        # ── YOUR EXIT LOGIC GOES HERE ──────────────────────────────

        # 1. Profit target
        profit_target = collected * config.PROFIT_TARGET_PCT
        if pnl >= profit_target:
            return True, "shortStrangle_target_hit ({:.0f} >= {:.0f})".format(pnl, profit_target)

        # 2. Stop loss
        stop_loss_val = collected * config.STOP_LOSS_MULTIPLIER
        if current_val >= stop_loss_val:
            return True, "shortStrangle_sl_hit (cost {:.0f} >= SL {:.0f})".format(current_val, stop_loss_val)

        # 3. Expiry day close-out (before 3 PM)
        from datetime import datetime, date
        for leg in trade.legs:
            try:
                expiry_date = datetime.strptime(leg.expiry, "%d%b%Y").date()
                if expiry_date == date.today() and datetime.now().hour >= 15:
                    return True, "shortStrangle_expiry_day_close"
            except ValueError:
                pass

        return False, ""

    # ──────────────────────────────────────────────────────────────
    # ADJUSTMENT DONE
    # Called when exit_criteria returns True, before the bot closes legs.
    # Use this to roll strikes, add hedges, or modify the trade instead
    # of a straight close.
    #
    # context keys available:
    #   trade, ltps, instrument, option_chain, broker, exit_reason
    #
    # Return False → bot proceeds with normal close (all legs)
    # Return True  → adjustment applied, bot skips the close this cycle
    # ──────────────────────────────────────────────────────────────
    def adjustment_done(self, context):

        trade       = context["trade"]
        exit_reason = context.get("exit_reason", "")
        broker      = context["broker"]

        # ── YOUR ADJUSTMENT LOGIC GOES HERE ───────────────────────
        # Example: if SL hit, roll the losing leg instead of closing
        #
        # if "sl_hit" in exit_reason:
        #     losing_leg = max(trade.legs, key=lambda l: l.exit_premium or 0)
        #     # ... place roll order via broker ...
        #     logger.info("[shortStrangle] Rolled losing leg %s", losing_leg.symbol)
        #     return True   # tell bot: adjustment done, don't close yet
        #
        # For now, no adjustment — always do a straight close.

        logger.info("[shortStrangle] No adjustment for trade %s (reason: %s)",
                    trade.trade_id, exit_reason)
        return False
