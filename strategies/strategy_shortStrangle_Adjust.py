from __future__ import annotations

"""
strategy_shortStrangle_Adjust.py — Strategy: "shortStrangle_Adjust"

Moved to strategies/ folder.

Entry  : sell OTM CE + PE near TARGET_DELTA
Exit   : 70% of max profit, or Friday 3:16 PM
Adjust : when |CE_ltp - PE_ltp| * qty > adj_entry_premium (the Rs. threshold)

State stored on the Trade object:
  trade.adj_entry_premium  — Rs. threshold = ADJUST_THRESHOLD * total_premium_collected
                              Recalculated after every adjustment from all open legs.
  trade.adj_count          — number of adjustments completed
  trade.adj_ce_strike_low  — lowest CE strike allowed (= original PE strike)
  trade.adj_pe_strike_high — highest PE strike allowed (= original CE strike)
  trade.adj_straddle       — True once CE strike == PE strike
"""

import logging
import time
from datetime import datetime, date

# strategy_base is a sibling file inside this package
from strategies.strategy_base import BaseStrategy, EntrySignal

import config

logger = logging.getLogger(__name__)

STRATEGY_NAME    = "shortStrangle_Adjust"
ADJUST_THRESHOLD = 0.40


class ShortStrangleAdjustStrategy(BaseStrategy):

    NAME        = STRATEGY_NAME
    DESCRIPTION = "Short strangle with rolling adjustment on premium imbalance"

    @staticmethod
    def _step(instrument):
        return 50 if instrument == "NIFTY" else 100

    # ── Entry ──────────────────────────────────────────────────────────

    def entry_criteria(self, context):
        instrument  = context["instrument"]
        oc          = context["option_chain"]
        open_trades = context["open_trades"]

        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in open_trades
        )
        if already_in:
            logger.info("[%s][%s] Already in position — skip entry",
                        instrument, STRATEGY_NAME)
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
            logger.info("[%s][%s] No strikes in delta range — skip",
                        instrument, STRATEGY_NAME)
            return None

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
                    "order_type":  "MARKET",
                    "product":     "NRML",
                },
                {
                    "security_id": int(pe_row["PE SECURITY_ID"]),
                    "option_type": "PE",
                    "transaction": "SELL",
                    "ltp":         pe_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                    "order_type":  "MARKET",
                    "product":     "NRML",
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
        profit_target = collected * 0.70

        if pnl >= profit_target:
            return True, "{}_target_70pct ({:.0f} >= {:.0f})".format(
                STRATEGY_NAME, pnl, profit_target)

        now = datetime.now()
        if now.weekday() == 4 and now.hour == 15 and now.minute >= 16:
            return True, "{}_friday_expiry_close".format(STRATEGY_NAME)

        return False, ""

    def adjustment_done(self, context):
        return False

    # ── Check and adjust ───────────────────────────────────────────────

    def check_and_adjust(self, context):
        """
        Called every scan cycle BEFORE exit_criteria.

        Returns (adjusted, closed_leg, new_leg, reset_signal).
        reset_signal is a dict when a full straddle reset is triggered,
        None for normal single-leg adjustments and no-ops.
        """
        trade  = context["trade"]
        ltps   = context["ltps"]
        oc     = context["option_chain"]
        broker = context["broker"]

        # ── Initialise state on first call ─────────────────────────────
        if not hasattr(trade, "adj_entry_premium"):
            ce_leg = self._ce_leg(trade)
            pe_leg = self._pe_leg(trade)
            if ce_leg is None or pe_leg is None:
                return False, None, None, None

            trade.adj_entry_premium  = ADJUST_THRESHOLD * trade.total_premium_collected
            trade.adj_count          = 0
            trade.adj_straddle       = False
            trade.adj_ce_strike_low  = pe_leg.strike
            trade.adj_pe_strike_high = ce_leg.strike

            logger.info(
                "[%s][%s] Adjustment state initialised | "
                "total_premium=Rs.%.0f | threshold=Rs.%.0f",
                trade.trade_id, STRATEGY_NAME,
                trade.total_premium_collected,
                trade.adj_entry_premium,
            )

        # ── Already at straddle — act on STRADDLE_ACTION ───────────────
        if trade.adj_straddle:
            action = getattr(config, "STRADDLE_ACTION", "hold")
            if action == "reset_next_expiry":
                return self._straddle_reset(context)
            logger.info("[%s][%s] Already at straddle — holding (action=%s)",
                        trade.trade_id, STRATEGY_NAME, action)
            return False, None, None, None

        ce_leg = self._ce_leg(trade)
        pe_leg = self._pe_leg(trade)
        if ce_leg is None or pe_leg is None:
            return False, None, None, None

        ce_ltp = ltps.get(ce_leg.symbol, ce_leg.entry_price)
        pe_ltp = ltps.get(pe_leg.symbol, pe_leg.entry_price)
        qty    = ce_leg.quantity

        # ── Trigger check (all values in total Rs.) ────────────────────
        imbalance_total = abs(ce_ltp - pe_ltp) * qty
        if imbalance_total <= trade.adj_entry_premium:
            logger.info(
                "[%s][%s] NO Adjustment: CE=%.1f PE=%.1f imbalance=Rs.%d threshold=Rs.%d",
                trade.trade_id, STRATEGY_NAME,
                ce_ltp, pe_ltp,
                round(imbalance_total), round(trade.adj_entry_premium),
            )
            return False, None, None, None

        logger.info(
            "[%s][%s] Adjustment triggered: "
            "CE_ltp=%.1f PE_ltp=%.1f imbalance_Rs=%.0f threshold_Rs=%.0f (adj#%d)",
            trade.trade_id, STRATEGY_NAME,
            ce_ltp, pe_ltp, imbalance_total, trade.adj_entry_premium,
            trade.adj_count + 1,
        )

        # ── Identify profitable leg ────────────────────────────────────
        if ce_ltp < pe_ltp:
            profit_leg = ce_leg
            target_ltp = pe_ltp
            roll_side  = "CE"
        else:
            profit_leg = pe_leg
            target_ltp = ce_ltp
            roll_side  = "PE"

        # ── Find new strike ────────────────────────────────────────────
        new_row = self._find_strike_by_ltp(
            oc                = oc,
            option_type       = roll_side,
            target_ltp        = target_ltp,
            ce_floor          = trade.adj_ce_strike_low,
            pe_ceiling        = trade.adj_pe_strike_high,
            current_ce_strike = ce_leg.strike,
            current_pe_strike = pe_leg.strike,
            roll_side         = roll_side,
        )

        if new_row is None:
            logger.warning("[%s][%s] No valid strike for roll — skip adjustment",
                           trade.trade_id, STRATEGY_NAME)
            return False, None, None, None

        new_strike = int(new_row["Strike Price"])
        new_sec_id = int(new_row["{} SECURITY_ID".format(roll_side)])
        new_ltp    = float(new_row["{} LTP".format(roll_side)])
        new_symbol = broker.get_security_name(new_sec_id)
        product    = new_row.get("product", "NRML")
        order_type = new_row.get("order_type", "MARKET")

        # ── Straddle guard — dispatch to STRADDLE_ACTION ───────────────
        if roll_side == "CE":
            will_be_straddle = (new_strike <= pe_leg.strike)
        else:
            will_be_straddle = (new_strike >= ce_leg.strike)

        if will_be_straddle:
            trade.adj_straddle = True
            action = getattr(config, "STRADDLE_ACTION", "hold")
            if action == "reset_next_expiry":
                logger.info(
                    "[%s][%s] Straddle boundary reached — dispatching reset_next_expiry",
                    trade.trade_id, STRATEGY_NAME,
                )
                return self._straddle_reset(context)
            # "hold" — clamp to the straddle strike
            logger.info("[%s][%s] Straddle boundary reached — clamping (action=%s)",
                        trade.trade_id, STRATEGY_NAME, action)
            straddle_strike = pe_leg.strike if roll_side == "CE" else ce_leg.strike
            straddle_row    = oc[oc["Strike Price"] == straddle_strike]
            if straddle_row.empty:
                logger.warning("[%s][%s] Straddle strike %d not in chain",
                               trade.trade_id, STRATEGY_NAME, straddle_strike)
                return False, None, None, None
            row_data   = straddle_row.iloc[0]
            new_strike = straddle_strike
            new_sec_id = int(row_data["{} SECURITY_ID".format(roll_side)])
            new_ltp    = float(row_data["{} LTP".format(roll_side)])
            new_symbol = broker.get_security_name(new_sec_id)
            tradingsymbol = broker.get_option_symbol(new_sec_id)
           

        # ── Exchange for order placement ───────────────────────────────
        order_exchange = "NFO" if trade.exchange == "INDEX" else trade.exchange

        # ── Close profitable leg ───────────────────────────────────────
        logger.info("[%s][%s] Closing profitable %s leg %s @ Rs.%.1f",
                    trade.trade_id, STRATEGY_NAME,
                    roll_side, profit_leg.symbol, ltps.get(profit_leg.symbol, 0))

        # close_order_id = broker.place_buy_order(
        #     profit_leg.symbol, profit_leg.quantity, exchange=order_exchange
        # )
        close_order_id = broker.place_buy_order(strategryName=self.strategy.NAME, tradingsymbol=profit_leg.tradingsymbol, 
                                                quantity=qty, product=product, order_type=order_type,
                                                exchange=order_exchange)
        if not close_order_id:
            logger.error("[%s][%s] Failed to close %s leg — aborting adjustment",
                         trade.trade_id, STRATEGY_NAME, roll_side)
            return False, None, None, None

        profit_leg.exit_premium = broker.get_executed_price(
            close_order_id,
            paper_ltp=ltps.get(profit_leg.symbol, profit_leg.entry_price),
        )
        profit_leg.status = "CLOSED"
        time.sleep(1)

        # ── Sell new strike ────────────────────────────────────────────
        logger.info("[%s][%s] Selling new %s strike %d @ Rs.%.1f",
                    trade.trade_id, STRATEGY_NAME, roll_side, new_strike, new_ltp)

        # new_order_id = broker.place_sell_order(
        #     new_symbol, profit_leg.quantity, exchange=order_exchange
        # )
        new_order_id = broker.place_sell_order(strategryName=self.strategy.NAME, tradingsymbol=tradingsymbol, 
                                                quantity=qty, product=product, order_type=order_type,
                                                exchange=order_exchange)

        if not new_order_id:
            logger.error("[%s][%s] Failed to open new %s leg — adjustment incomplete",
                         trade.trade_id, STRATEGY_NAME, roll_side)
            return False, None, None, None

        new_fill = broker.get_executed_price(new_order_id, paper_ltp=new_ltp)
        time.sleep(1)

        # ── Replace closed leg in trade.legs ───────────────────────────
        # Import OptionLeg from the top-level strategies module.
        # sys.path includes the project root (set by options_bot.py at startup),
        # so this resolves to strategies.py in the repo root, not this package.
        import strategies as _strategies
        OptionLeg = _strategies.OptionLeg

        new_leg = OptionLeg(
            symbol        = new_symbol,
            tradingsymbol = tradingsymbol,
            instrument    = trade.instrument,
            exchange      = trade.exchange,
            expiry        = profit_leg.expiry,
            strike        = new_strike,
            option_type   = roll_side,
            transaction   = "SELL",
            lots          = profit_leg.lots,
            quantity      = profit_leg.quantity,
            entry_price   = new_fill,
            entry_premium = new_fill * profit_leg.quantity,
            order_id      = new_order_id,
        )

        trade.legs = [l for l in trade.legs if l is not profit_leg] + [new_leg]
        trade.adj_count += 1

        # ── Recalculate threshold from all currently open legs ─────────
        new_total_premium       = sum(
            l.entry_premium for l in trade.legs if l.status == "OPEN"
        )
        trade.adj_entry_premium = ADJUST_THRESHOLD * new_total_premium

        logger.info(
            "[%s][%s] Adjustment #%d complete: new %s %s strike=%d "
            "entry_price=Rs.%.1f entry_premium=Rs.%.0f | "
            "new_total_premium=Rs.%.0f | new_threshold=Rs.%.0f | Straddle=%s",
            trade.trade_id, STRATEGY_NAME, trade.adj_count,
            roll_side, new_symbol, new_strike,
            new_fill, new_fill * profit_leg.quantity,
            new_total_premium, trade.adj_entry_premium,
            trade.adj_straddle,
        )

        return True, profit_leg, new_leg, None

    # ── Straddle reset ─────────────────────────────────────────────────

    def _straddle_reset(self, context):
        """
        Close ALL open legs, then return a reset_signal dict so
        options_bot.py can attempt a fresh entry on the next expiry.
        """
        trade  = context["trade"]
        ltps   = context["ltps"]
        broker = context["broker"]

        order_exchange = "NFO" if trade.exchange == "INDEX" else trade.exchange
        no_entry_day   = getattr(config, "STRADDLE_NO_ENTRY_WEEKDAY", 4)
        #is_blocked_day = (datetime.now().weekday() == no_entry_day)
        is_blocked_day = True  # BLOCK ALL RE-ENTRIES for now, to avoid confusion during testing

        logger.info(
            "[%s][%s] Straddle reset: closing all %d open leg(s)%s",
            trade.trade_id, STRATEGY_NAME,
            sum(1 for l in trade.legs if l.status == "OPEN"),
            " | re-entry BLOCKED (blocked weekday)" if is_blocked_day else "",
        )

        all_closed = True
        for leg in trade.legs:
            if leg.status != "OPEN":
                continue
            order_id = broker.place_buy_order(
                leg.symbol, leg.quantity, exchange=order_exchange
            )
            if order_id:
                leg.exit_premium = broker.get_executed_price(
                    order_id,
                    paper_ltp=ltps.get(leg.symbol, leg.entry_price),
                )
                leg.status = "CLOSED"
                logger.info(
                    "[%s][%s] Reset: closed %s %s @ Rs.%.1f",
                    trade.trade_id, STRATEGY_NAME,
                    leg.option_type, leg.symbol,
                    leg.exit_premium or 0,
                )
            else:
                all_closed = False
                logger.error(
                    "[%s][%s] Reset: failed to close leg %s — aborting reset",
                    trade.trade_id, STRATEGY_NAME, leg.symbol,
                )

        if not all_closed:
            trade.adj_straddle = False
            return False, None, None, None

        reset_signal = {
            "instrument":   trade.instrument,
            "exchange":     trade.exchange,
            "expiry_index": config.EXPIRY_INDEX + 1,
            "lots":         max(l.lots for l in trade.legs) if trade.legs
                            else config.MAX_LOTS_PER_TRADE,
            "re_entry":     not is_blocked_day,
        }

        logger.info(
            "[%s][%s] Reset complete | re_entry=%s | next expiry index=%d",
            trade.trade_id, STRATEGY_NAME,
            reset_signal["re_entry"], reset_signal["expiry_index"],
        )

        return True, None, None, reset_signal

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _ce_leg(trade):
        return next(
            (l for l in trade.legs if l.option_type == "CE" and l.status == "OPEN"),
            None,
        )

    @staticmethod
    def _pe_leg(trade):
        return next(
            (l for l in trade.legs if l.option_type == "PE" and l.status == "OPEN"),
            None,
        )

    @staticmethod
    def _find_strike_by_ltp(oc, option_type, target_ltp,
                             ce_floor, pe_ceiling,
                             current_ce_strike, current_pe_strike,
                             roll_side):
        ltp_col = "{} LTP".format(option_type)

        if roll_side == "CE":
            candidates = oc[
                (oc["Strike Price"] <  current_ce_strike) &
                (oc["Strike Price"] >= ce_floor) &
                (oc[ltp_col].notna()) &
                (oc[ltp_col] > 0)
            ].copy()
        else:
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
