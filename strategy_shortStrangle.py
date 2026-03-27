from __future__ import annotations

"""
strategy_shortStrangle.py — Strategy: "shortStrangle"

Delta-based short strangle without adjustments.
Exchange is always taken from context["exchange"] — never hardcoded.
"""

import logging
from datetime import datetime, date

from strategy_base import BaseStrategy, EntrySignal
import config

logger = logging.getLogger(__name__)


class ShortStrangleStrategy(BaseStrategy):

    NAME        = "shortStrangle"
    DESCRIPTION = "Delta-based short strangle without adjustments"

    # ── Entry ──────────────────────────────────────────────────────────

    def entry_criteria(self, context):
        instrument  = context["instrument"]
        # exchange is available in context but not needed here — broker
        # calls are made in options_bot.py which handles the exchange routing.
        oc          = context["option_chain"]
        open_trades = context["open_trades"]

        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in open_trades
        )
        if already_in:
            logger.info("[%s][shortStrangle] Already in position — skip entry", instrument)
            return None

        ce_candidates = oc[
            (oc["CE Delta"] >= config.TARGET_DELTA - 0.05) &
            (oc["CE Delta"] <= config.TARGET_DELTA + 0.05)
        ]
        pe_candidates = oc[
            (oc["PE Delta"].abs() >= config.TARGET_DELTA - 0.05) &
            (oc["PE Delta"].abs() <= config.TARGET_DELTA + 0.05)
        ]

        if ce_candidates.empty or pe_candidates.empty:
            logger.info("[%s][shortStrangle] No strikes in delta range — skip", instrument)
            return None

        ce_row = ce_candidates.sort_values("CE OI").iloc[-1]
        pe_row = pe_candidates.sort_values("PE OI").iloc[-1]

        ce_ltp = float(ce_row["CE LTP"])
        pe_ltp = float(pe_row["PE LTP"])

        if (ce_ltp + pe_ltp) < config.MIN_PREMIUM:
            logger.info("[%s][shortStrangle] Combined premium Rs.%.1f below MIN — skip",
                        instrument, ce_ltp + pe_ltp)
            return None

        logger.info("[%s][shortStrangle] Entry signal: CE %d @ Rs.%.1f | PE %d @ Rs.%.1f",
                    instrument,
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

    # ── Exit ───────────────────────────────────────────────────────────

    def exit_criteria(self, context):
        trade = context["trade"]
        ltps  = context["ltps"]

        for leg in trade.legs:
            if leg.symbol in ltps:
                leg.exit_premium = ltps[leg.symbol]

        collected     = trade.total_premium_collected
        pnl           = trade.pnl
        profit_target = collected * config.PROFIT_TARGET_PCT

        if pnl >= profit_target:
            return True, "shortStrangle_target_hit ({:.0f} >= {:.0f})".format(
                pnl, profit_target)

        stop_loss_val = collected * config.STOP_LOSS_MULTIPLIER
        if trade.current_premium >= stop_loss_val:
            return True, "shortStrangle_sl_hit (cost {:.0f} >= SL {:.0f})".format(
                trade.current_premium, stop_loss_val)

        for leg in trade.legs:
            try:
                expiry_date = datetime.strptime(leg.expiry, "%d%b%Y").date()
                if expiry_date == date.today() and datetime.now().hour >= 15:
                    return True, "shortStrangle_expiry_day_close"
            except ValueError:
                pass

        return False, ""

    # ── Adjustment ─────────────────────────────────────────────────────

    def adjustment_done(self, context):
        logger.info("[shortStrangle] No adjustment for trade %s (reason: %s)",
                    context["trade"].trade_id, context.get("exit_reason", ""))
        return False