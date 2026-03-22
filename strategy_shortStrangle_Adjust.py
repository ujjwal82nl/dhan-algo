from __future__ import annotations

"""
strategy_shortStrangle_Adjust.py — Strategy: "shortStrangle_Adjust"

Entry  : same as shortStrangle — sell OTM CE + PE near TARGET_DELTA
Exit   : 70% of max profit reached, OR Friday 3:16 PM before expiry
Adjust : when |CE_ltp - PE_ltp| > 40% of combined entry premium
           → close the profitable leg (the one that moved in our favour)
           → re-sell it at a new strike whose LTP ≈ the losing leg's current LTP
           → guard: CE strike must never go below PE strike (straddle = last state)
           → once straddle reached, no further adjustments

State stored directly on the Trade object (Python dataclasses allow extra attrs):
    trade.adj_entry_premium   — combined premium at entry (for 40% trigger)
    trade.adj_count           — number of adjustments done so far
    trade.adj_ce_strike_low   — lowest CE strike allowed (= original PE strike + step)
    trade.adj_pe_strike_high  — highest PE strike allowed (= original CE strike - step)
    trade.adj_straddle        — True once CE strike == PE strike, no more adjustments
"""

import logging
import time
from datetime import datetime, date

from strategy_base import BaseStrategy, EntrySignal
import config

logger = logging.getLogger(__name__)

STRATEGY_NAME = "shortStrangle_Adjust"
ADJUST_THRESHOLD = 0.40   # trigger when leg premium diff > 40% of entry premium


class ShortStrangleAdjustStrategy(BaseStrategy):

    NAME        = STRATEGY_NAME
    DESCRIPTION = "Short strangle with rolling adjustment on premium imbalance"

    # ── Strike step sizes ──────────────────────────────────────────
    @staticmethod
    def _step(instrument):
        return 50 if instrument == "NIFTY" else 100

    # ─────────────────────────────────────────────────────────────
    # ENTRY CRITERIA  (identical to shortStrangle)
    # ─────────────────────────────────────────────────────────────
    def entry_criteria(self, context):

        instrument  = context["instrument"]
        oc          = context["option_chain"]
        open_trades = context["open_trades"]

        # Guard: skip if already in this instrument
        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in open_trades
        )
        if already_in:
            logger.info("[%s][%s] Already in position — skip entry",
                        instrument, STRATEGY_NAME)
            return None

        # Filter by delta range
        ce_candidates = oc[
            (oc["CE Delta"] >= config.TARGET_DELTA - 0.05) &
            (oc["CE Delta"] <= config.TARGET_DELTA + 0.05)
        ]
        pe_candidates = oc[
            (oc["PE Delta"].abs() >= config.TARGET_DELTA - 0.05) &
            (oc["PE Delta"].abs() <= config.TARGET_DELTA + 0.05)
        ]

        if ce_candidates.empty or pe_candidates.empty:
            logger.info("[%s][%s] No strikes in delta range — skip entry",
                        instrument, STRATEGY_NAME)
            return None

        # Highest OI on each side
        ce_row = ce_candidates.sort_values("CE OI").iloc[-1]
        pe_row = pe_candidates.sort_values("PE OI").iloc[-1]

        ce_ltp = float(ce_row["CE LTP"])
        pe_ltp = float(pe_row["PE LTP"])

        if (ce_ltp + pe_ltp) < config.MIN_PREMIUM:
            logger.info("[%s][%s] Combined premium Rs.%.1f below MIN — skip",
                        instrument, STRATEGY_NAME, ce_ltp + pe_ltp)
            return None

        logger.info("[%s][%s] Entry signal: CE %d @ Rs.%.1f | PE %d @ Rs.%.1f",
                    instrument, STRATEGY_NAME,
                    int(ce_row["Strike Price"]), ce_ltp,
                    int(pe_row["Strike Price"]), pe_ltp)

        return EntrySignal(
            strategy_name=self.NAME,
            legs=[
                {
                    "security_id": int(ce_row["CE SECURITY_ID"]),
                    "option_type": "CE",
                    "transaction": "SELL",
                    "ltp":         ce_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                },
                {
                    "security_id": int(pe_row["PE SECURITY_ID"]),
                    "option_type": "PE",
                    "transaction": "SELL",
                    "ltp":         pe_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                },
            ],
        )

    # ─────────────────────────────────────────────────────────────
    # EXIT CRITERIA
    # ─────────────────────────────────────────────────────────────
    def exit_criteria(self, context):

        trade = context["trade"]
        ltps  = context["ltps"]

        # Update live exit premiums on legs
        for leg in trade.legs:
            if leg.symbol in ltps:
                leg.exit_premium = ltps[leg.symbol]

        collected = trade.total_premium_collected
        pnl       = trade.pnl

        # 1. Profit target: 70% of max profit
        profit_target = collected * 0.70
        if pnl >= profit_target:
            return True, "{}_target_70pct ({:.0f} >= {:.0f})".format(
                STRATEGY_NAME, pnl, profit_target)

        # 2. Friday 3:16 PM — force-close just before weekly expiry
        now = datetime.now()
        if now.weekday() == 4 and now.hour == 15 and now.minute >= 16:
            return True, "{}_friday_expiry_close".format(STRATEGY_NAME)

        return False, ""

    # ─────────────────────────────────────────────────────────────
    # ADJUSTMENT DONE
    # Called every cycle when exit_criteria has NOT triggered.
    # The bot calls this separately via monitor_adjustments() —
    # see options_bot.py note below.
    #
    # But it is ALSO called when exit_criteria returns True, before
    # the bot closes legs. In that case we return False (no adjustment
    # on a real exit — let the bot close normally).
    # ─────────────────────────────────────────────────────────────
    def adjustment_done(self, context):
        """
        Called after exit_criteria triggers. At that point we do NOT
        adjust — we let the bot close the trade normally.
        The actual adjustment check happens in check_and_adjust().
        """
        return False   # no adjustment on exit — close normally

    # ─────────────────────────────────────────────────────────────
    # CHECK AND ADJUST  (called every scan cycle from options_bot)
    # ─────────────────────────────────────────────────────────────
    def check_and_adjust(self, context):
        """
        Check whether an adjustment is needed for an open trade.
        Called each scan cycle BEFORE exit_criteria.

        Returns True if an adjustment was executed (bot should skip
        exit check this cycle), False otherwise.

        Adjustment rules:
          Trigger : |ce_ltp - pe_ltp| > ADJUST_THRESHOLD * adj_entry_premium
          Action  : close the profitable leg, re-sell at a strike where
                    new_ltp ≈ losing leg's current ltp
          Guard   : CE strike >= PE strike at all times
                    once they meet (straddle) → no more adjustments
        """
        trade  = context["trade"]
        ltps   = context["ltps"]
        oc     = context["option_chain"]
        broker = context["broker"]

        # ── Initialise state on first call for this trade ──────────
        if not hasattr(trade, "adj_entry_premium"):
            ce_leg = self._ce_leg(trade)
            pe_leg = self._pe_leg(trade)
            if ce_leg is None or pe_leg is None:
                return False
            trade.adj_entry_premium  = ce_leg.entry_premium + pe_leg.entry_premium
            trade.adj_count          = 0
            trade.adj_straddle       = False
            # Strike boundaries — CE must stay >= original PE strike,
            # PE must stay <= original CE strike (they converge inward)
            step = self._step(trade.instrument)
            trade.adj_ce_strike_low  = pe_leg.strike            # CE floor
            trade.adj_pe_strike_high = ce_leg.strike            # PE ceiling

        # ── No more adjustments once straddle is reached ───────────
        if trade.adj_straddle:
            logger.info("[%s][%s] Already at straddle — no further adjustments",
                        trade.trade_id, STRATEGY_NAME)
            return False

        ce_leg = self._ce_leg(trade)
        pe_leg = self._pe_leg(trade)
        if ce_leg is None or pe_leg is None:
            return False

        ce_ltp = ltps.get(ce_leg.symbol, ce_leg.entry_premium)
        pe_ltp = ltps.get(pe_leg.symbol, pe_leg.entry_premium)

        # ── Check trigger: premium imbalance > 40% of entry premium ─
        imbalance = abs(ce_ltp - pe_ltp)
        trigger   = ADJUST_THRESHOLD * trade.adj_entry_premium

        if imbalance <= trigger:
            return False   # premiums balanced — no adjustment needed

        logger.info(
            "[%s][%s] Adjustment triggered: CE_ltp=%.1f PE_ltp=%.1f "
            "imbalance=%.1f trigger=%.1f (adj#%d)",
            trade.trade_id, STRATEGY_NAME,
            ce_ltp, pe_ltp, imbalance, trigger, trade.adj_count + 1
        )

        # ── Identify profitable leg (lower current ltp = profit for seller) ─
        if ce_ltp < pe_ltp:
            # CE is profitable — roll CE inward (lower strike, closer to ATM)
            profit_leg   = ce_leg
            target_ltp   = pe_ltp            # new CE ltp should match current PE ltp
            roll_side    = "CE"
        else:
            # PE is profitable — roll PE inward (higher strike, closer to ATM)
            profit_leg   = pe_leg
            target_ltp   = ce_ltp            # new PE ltp should match current CE ltp
            roll_side    = "PE"

        # ── Find new strike on the option chain ────────────────────
        new_row = self._find_strike_by_ltp(
            oc           = oc,
            option_type  = roll_side,
            target_ltp   = target_ltp,
            ce_floor     = trade.adj_ce_strike_low,
            pe_ceiling   = trade.adj_pe_strike_high,
            current_ce_strike = ce_leg.strike,
            current_pe_strike = pe_leg.strike,
            roll_side    = roll_side,
            instrument   = trade.instrument,
        )

        if new_row is None:
            logger.warning("[%s][%s] No valid strike found for roll — skip adjustment",
                           trade.trade_id, STRATEGY_NAME)
            return False

        new_strike     = int(new_row["Strike Price"])
        new_sec_id     = int(new_row["{} SECURITY_ID".format(roll_side)])
        new_ltp        = float(new_row["{} LTP".format(roll_side)])
        new_symbol     = broker.get_security_name(new_sec_id)

        # ── Straddle guard: will this roll make CE strike == PE strike? ─
        if roll_side == "CE":
            will_be_straddle = (new_strike <= pe_leg.strike)
            final_ce_strike  = max(new_strike, pe_leg.strike)
        else:
            will_be_straddle = (new_strike >= ce_leg.strike)
            final_pe_strike  = min(new_strike, ce_leg.strike)

        if will_be_straddle:
            logger.info("[%s][%s] This adjustment would create a straddle — "
                        "clamping to straddle strike, no further adjustments after this.",
                        trade.trade_id, STRATEGY_NAME)
            trade.adj_straddle = True
            # Use the opposite leg's strike to form the straddle
            if roll_side == "CE":
                straddle_strike = pe_leg.strike
                straddle_row    = oc[oc["Strike Price"] == straddle_strike]
                if straddle_row.empty:
                    logger.warning("[%s][%s] Straddle strike %d not in chain",
                                   trade.trade_id, STRATEGY_NAME, straddle_strike)
                    return False
                new_row      = straddle_row.iloc[0]
                new_strike   = straddle_strike
                new_sec_id   = int(new_row["CE SECURITY_ID"])
                new_ltp      = float(new_row["CE LTP"])
                new_symbol   = broker.get_security_name(new_sec_id)
            else:
                straddle_strike = ce_leg.strike
                straddle_row    = oc[oc["Strike Price"] == straddle_strike]
                if straddle_row.empty:
                    logger.warning("[%s][%s] Straddle strike %d not in chain",
                                   trade.trade_id, STRATEGY_NAME, straddle_strike)
                    return False
                new_row      = straddle_row.iloc[0]
                new_strike   = straddle_strike
                new_sec_id   = int(new_row["PE SECURITY_ID"])
                new_ltp      = float(new_row["PE LTP"])
                new_symbol   = broker.get_security_name(new_sec_id)

        # ── Execute: BUY back (close) the profitable leg ───────────
        logger.info("[%s][%s] Closing profitable %s leg %s @ Rs.%.1f",
                    trade.trade_id, STRATEGY_NAME,
                    roll_side, profit_leg.symbol, ltps.get(profit_leg.symbol, 0))

        close_order_id = broker.place_buy_order(profit_leg.symbol, profit_leg.quantity)
        if not close_order_id:
            logger.error("[%s][%s] Failed to close %s leg — aborting adjustment",
                         trade.trade_id, STRATEGY_NAME, roll_side)
            return False

        profit_leg.exit_premium = broker.get_executed_price(
            close_order_id, paper_ltp=ltps.get(profit_leg.symbol, profit_leg.entry_premium)
        )
        profit_leg.status = "CLOSED"
        time.sleep(1)

        # ── Execute: SELL new strike ────────────────────────────────
        logger.info("[%s][%s] Selling new %s strike %d @ Rs.%.1f",
                    trade.trade_id, STRATEGY_NAME, roll_side, new_strike, new_ltp)

        new_order_id = broker.place_sell_order(new_symbol, profit_leg.quantity)
        if not new_order_id:
            logger.error("[%s][%s] Failed to open new %s leg — adjustment incomplete",
                         trade.trade_id, STRATEGY_NAME, roll_side)
            return False

        new_fill = broker.get_executed_price(new_order_id, paper_ltp=new_ltp)
        time.sleep(1)

        # ── Replace the closed leg with the new one in trade.legs ──
        from strategies import OptionLeg
        new_leg = OptionLeg(
            symbol        = new_symbol,
            instrument    = trade.instrument,
            expiry        = profit_leg.expiry,
            strike        = new_strike,
            option_type   = roll_side,
            lots          = profit_leg.lots,
            quantity      = profit_leg.quantity,
            entry_premium = new_fill,
            order_id      = new_order_id,
        )
        trade.legs = [l for l in trade.legs if l is not profit_leg] + [new_leg]

        trade.adj_count += 1
        logger.info(
            "[%s][%s] Adjustment #%d complete: new %s leg %s strike=%d @ Rs.%.1f | "
            "Straddle=%s",
            trade.trade_id, STRATEGY_NAME, trade.adj_count,
            roll_side, new_symbol, new_strike, new_fill,
            trade.adj_straddle
        )
        return True   # adjustment done — bot skips exit/close this cycle

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _ce_leg(trade):
        return next((l for l in trade.legs if l.option_type == "CE" and l.status == "OPEN"), None)

    @staticmethod
    def _pe_leg(trade):
        return next((l for l in trade.legs if l.option_type == "PE" and l.status == "OPEN"), None)

    @staticmethod
    def _find_strike_by_ltp(oc, option_type, target_ltp,
                             ce_floor, pe_ceiling,
                             current_ce_strike, current_pe_strike,
                             roll_side, instrument):
        """
        Find the strike on the option chain whose LTP is closest to target_ltp,
        subject to the boundary constraints so strikes never cross.

        CE rolls inward  → new CE strike must be >= ce_floor (original PE strike)
                           and < current CE strike (moving toward ATM)
        PE rolls inward  → new PE strike must be <= pe_ceiling (original CE strike)
                           and > current PE strike (moving toward ATM)
        """
        ltp_col = "{} LTP".format(option_type)

        if roll_side == "CE":
            # CE moves inward: new strike strictly less than current CE, >= floor
            candidates = oc[
                (oc["Strike Price"] <  current_ce_strike) &
                (oc["Strike Price"] >= ce_floor) &
                (oc[ltp_col].notna()) &
                (oc[ltp_col] > 0)
            ].copy()
        else:
            # PE moves inward: new strike strictly greater than current PE, <= ceiling
            candidates = oc[
                (oc["Strike Price"] >  current_pe_strike) &
                (oc["Strike Price"] <= pe_ceiling) &
                (oc[ltp_col].notna()) &
                (oc[ltp_col] > 0)
            ].copy()

        if candidates.empty:
            return None

        candidates["_ltp_dist"] = (candidates[ltp_col] - target_ltp).abs()
        return candidates.loc[candidates["_ltp_dist"].idxmin()]
