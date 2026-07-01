from __future__ import annotations

"""
strategy_intradayShortSell.py — Strategy: "intradayShortSell"

Instrument : NIFTY  (NSE INDEX)
Days       : Monday, Tuesday, Friday  (weekday 0, 1, 4)
Type       : Intraday — all positions squared off by 15:30

Phase timeline
--------------
09:16  BUY  far-OTM CE @ LTP ≈ 5          (cap / hedge)
       BUY  far-OTM PE @ LTP ≈ 5          (cap / hedge)
       SELL OTM-6 CE   with SL             (primary short)
       SELL OTM-6 PE   with SL             (primary short)

14:15  BUY  OTM-6 CE  (if SL not already hit)   ← close Phase-1 short
       BUY  OTM-6 PE  (if SL not already hit)
       SELL ATM CE    with SL                     ← Phase-2 short
       SELL ATM PE    with SL

15:28  BUY  ATM CE  (if SL not already hit)      ← close Phase-2 short
       BUY  ATM PE  (if SL not already hit)
       SELL far-OTM CE @ LTP ≈ 5                  ← hedges now become shorts
       SELL far-OTM PE @ LTP ≈ 5

SL rule
-------
Per SELL leg: SL = min(50% of entry_premium, Rs. 2000)
SL is placed as a separate SL-M order via OpenAlgo immediately after each SELL.
The SL order id is stored so we can check its status before the phase transition.

OpenAlgo order calls used
--------------------------
client.placeorder(...)             — place SELL / BUY market orders
client.placesmartorder(...)        — place SL-M order linked to the short leg
client.orderstatus(order_id, ...)  — check if SL has been triggered
client.quotes(symbol, exchange)    — fetch LTP for a symbol

"OTM-6" means 6 strike steps from ATM.
NIFTY step = 50 pts  →  OTM-6 = 300 pts from ATM
Far-OTM hedge target LTP ≈ 5 — we scan outward from OTM-10 until we find
the closest strike whose LTP is between 3 and 8.
"""

import logging
import time
from datetime import datetime, date

# strategy_base is a sibling file inside this package
from strategies.strategy_base import BaseStrategy, EntrySignal
import config

logger = logging.getLogger(__name__)

STRATEGY_NAME  = "intradayShortSell"
INSTRUMENT     = "NIFTY"
EXCHANGE       = "NSE"       # OpenAlgo exchange string for NIFTY options
OC_EXCHANGE    = "NSE_INDEX" # OpenAlgo exchange for option chain / quotes
NIFTY_STEP     = 50          # Strike increment for NIFTY
OTM6_STEPS     = 6           # Steps from ATM for Phase-1 shorts
FAR_OTM_LTP    = 5.0         # Target LTP for far-OTM hedge legs
FAR_OTM_RANGE  = (3.0, 8.0)  # Acceptable LTP band for far-OTM selection
SL_MAX_INR     = 2000        # Absolute cap on SL value per leg
SL_PCT         = 0.50        # SL = 50% of entry premium

# Active weekdays (Python: Mon=0 Tue=1 Wed=2 Thu=3 Fri=4)
ACTIVE_DAYS    = {0, 1, 4}   # Mon, Tue, Fri

# Phase transition times as (hour, minute) tuples
PHASE1_TIME    = (9,  16)
PHASE2_TIME    = (14, 15)
PHASE3_TIME    = (15, 25)
SQUAREOFF_TIME = (15, 28)    # Hard square-off — close everything remaining


# ── State keys stored on trade object ─────────────────────────────────────
#
# trade.iss_phase          int   0=not started, 1=active, 2=active, 3=active
# trade.iss_hedge_ce       OptionLeg   far-OTM CE bought at 09:16
# trade.iss_hedge_pe       OptionLeg   far-OTM PE bought at 09:16
# trade.iss_short_ce       OptionLeg   current active short CE leg
# trade.iss_short_pe       OptionLeg   current active short PE leg
# trade.iss_short_ce_sl_id str   SL order id for the short CE
# trade.iss_short_pe_sl_id str   SL order id for the short PE
# trade.iss_phase2_done    bool  True once phase-2 orders have been placed
# trade.iss_phase3_done    bool  True once phase-3 orders have been placed
# trade.iss_squared_off    bool  True once hard square-off is complete


class IntradayShortSellStrategy(BaseStrategy):

    NAME        = STRATEGY_NAME
    DESCRIPTION = (
        "Intraday 3-phase NIFTY short-sell: "
        "OTM-6 strangle (09:16) → ATM straddle (14:15) → far-OTM short (15:28). "
        "Active Mon / Tue / Fri only."
    )

    # ─────────────────────────────────────────────────────────────────
    # ENTRY CRITERIA
    # Called every scan cycle. Returns an EntrySignal only at 09:16
    # on active weekdays when no position exists yet.
    # The signal carries all 4 opening orders (2 BUY + 2 SELL).
    # ─────────────────────────────────────────────────────────────────

    def entry_criteria(self, context):
        instrument  = context["instrument"]
        oc          = context["option_chain"]
        atm_strike  = context["atm_strike"]
        open_trades = context["open_trades"]
        broker      = context["broker"]

        # ── Guard: only NIFTY, only active days ────────────────────
        if instrument != INSTRUMENT:
            return None

        today = date.today()
        if today.weekday() not in ACTIVE_DAYS:
            logger.info("[%s][%s] Not an active day (%s) — skip",
                        instrument, STRATEGY_NAME, today.strftime("%A"))
            return None

        # ── Guard: only fire at 09:16 ───────────────────────────────
        now = datetime.now()
        if not self._is_at(PHASE1_TIME, now):
            return None

        # ── Guard: no duplicate position ────────────────────────────
        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in open_trades
        )
        if already_in:
            logger.info("[%s][%s] Already in position — skip entry",
                        instrument, STRATEGY_NAME)
            return None

        # ── Resolve strikes ─────────────────────────────────────────
        otm6_ce_strike = atm_strike + OTM6_STEPS * NIFTY_STEP
        otm6_pe_strike = atm_strike - OTM6_STEPS * NIFTY_STEP

        # Find option chain rows for OTM-6 strikes
        otm6_ce_row = oc[oc["Strike Price"] == otm6_ce_strike]
        otm6_pe_row = oc[oc["Strike Price"] == otm6_pe_strike]

        if otm6_ce_row.empty or otm6_pe_row.empty:
            logger.error("[%s][%s] OTM-6 strikes (%d / %d) not in option chain",
                         instrument, STRATEGY_NAME, otm6_ce_strike, otm6_pe_strike)
            return None

        otm6_ce = otm6_ce_row.iloc[0]
        otm6_pe = otm6_pe_row.iloc[0]

        # Far-OTM hedge legs: scan outward from OTM-10 for LTP ≈ 5
        far_ce_row = self._find_far_otm(oc, "CE", atm_strike)
        far_pe_row = self._find_far_otm(oc, "PE", atm_strike)

        if far_ce_row is None or far_pe_row is None:
            logger.error("[%s][%s] Cannot find far-OTM hedge strikes with LTP ≈ %.1f",
                         instrument, STRATEGY_NAME, FAR_OTM_LTP)
            return None

        otm6_ce_ltp = float(otm6_ce["CE LTP"])
        otm6_pe_ltp = float(otm6_pe["PE LTP"])
        far_ce_ltp  = float(far_ce_row["CE LTP"])
        far_pe_ltp  = float(far_pe_row["PE LTP"])

        logger.info(
            "[%s][%s] Phase-1 entry at %02d:%02d | "
            "ATM=%d | OTM-6 CE %d@%.1f PE %d@%.1f | "
            "Far CE %d@%.1f PE %d@%.1f",
            instrument, STRATEGY_NAME, now.hour, now.minute,
            atm_strike,
            int(otm6_ce["Strike Price"]),  otm6_ce_ltp,
            int(otm6_pe["Strike Price"]),  otm6_pe_ltp,
            int(far_ce_row["Strike Price"]), far_ce_ltp,
            int(far_pe_row["Strike Price"]), far_pe_ltp,
        )

        # Return EntrySignal with all 4 Phase-1 legs.
        # The two BUY (hedge) legs come first so the protection is on
        # before the shorts are placed.
        return EntrySignal(
            strategy_name=self.NAME,
            legs=[
                # ── Hedge (protection) legs — BUY ──────────────────
                {
                    "security_id": int(far_ce_row["CE SECURITY_ID"]),
                    "option_type": "CE",
                    "transaction": "BUY",
                    "ltp":         far_ce_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                    "role":        "hedge_ce",       # extra key read by check_and_adjust
                },
                {
                    "security_id": int(far_pe_row["PE SECURITY_ID"]),
                    "option_type": "PE",
                    "transaction": "BUY",
                    "ltp":         far_pe_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                    "role":        "hedge_pe",
                },
                # ── Short legs — SELL with SL ───────────────────────
                {
                    "security_id": int(otm6_ce["CE SECURITY_ID"]),
                    "option_type": "CE",
                    "transaction": "SELL",
                    "ltp":         otm6_ce_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                    "role":        "short_ce",
                    "place_sl":    True,             # signal to options_bot to place SL order
                },
                {
                    "security_id": int(otm6_pe["PE SECURITY_ID"]),
                    "option_type": "PE",
                    "transaction": "SELL",
                    "ltp":         otm6_pe_ltp,
                    "lots":        config.MAX_LOTS_PER_TRADE,
                    "role":        "short_pe",
                    "place_sl":    True,
                },
            ],
        )

    # ─────────────────────────────────────────────────────────────────
    # EXIT CRITERIA
    # Normal exit: Friday 15:28 (all legs closed in Phase-3 rollover).
    # Hard exit: 15:30 square-off of anything still open.
    # ─────────────────────────────────────────────────────────────────

    def exit_criteria(self, context):
        trade = context["trade"]
        now   = datetime.now()
        ltps  = context["ltps"]

        # Update exit premiums on all open legs
        for leg in trade.legs:
            if leg.symbol in ltps and leg.status == "OPEN":
                leg.exit_premium = ltps[leg.symbol]

        # Hard square-off at 15:30 — close anything still open
        if now.hour == 15 and now.minute >= 30:
            remaining = [l for l in trade.legs if l.status == "OPEN"]
            if remaining:
                return True, "intradayShortSell_squareoff_1530"

        # If Phase-3 rollover already completed, all legs should be
        # closed — the trade itself is done
        if getattr(trade, "iss_phase3_done", False):
            all_closed = all(l.status == "CLOSED" for l in trade.legs)
            if all_closed:
                return True, "intradayShortSell_phase3_complete"

        return False, ""

    # ─────────────────────────────────────────────────────────────────
    # ADJUSTMENT DONE
    # Called after exit_criteria triggers. We always return False —
    # the full close is handled by the bot. Phase transitions are
    # managed in check_and_adjust(), not here.
    # ─────────────────────────────────────────────────────────────────

    def adjustment_done(self, context):
        return False

    # ─────────────────────────────────────────────────────────────────
    # CHECK AND ADJUST
    # Called every scan cycle BEFORE exit_criteria.
    # Drives the phase-2 (14:15) and phase-3 (15:28) transitions.
    # Returns (True, closed_leg, new_leg) if a transition was executed,
    # (False, None, None) otherwise.
    # ─────────────────────────────────────────────────────────────────

    def check_and_adjust(self, context):
        trade  = context["trade"]
        ltps   = context["ltps"]
        oc     = context["option_chain"]
        broker = context["broker"]
        now    = datetime.now()

        # ── Initialise phase state on first call ────────────────────
        if not hasattr(trade, "iss_phase"):
            trade.iss_phase       = 1
            trade.iss_phase2_done = False
            trade.iss_phase3_done = False
            trade.iss_squared_off = False
            # Map legs by role for easy access
            self._cache_legs(trade)
            logger.info("[%s][%s] Phase-1 active — monitoring SL and phase timers",
                        trade.trade_id, STRATEGY_NAME)

        # ── Already squared off — nothing to do ─────────────────────
        if trade.iss_squared_off:
            return False, None, None

        # ── Phase-2 transition at 14:15 ─────────────────────────────
        if not trade.iss_phase2_done and self._is_at_or_after(PHASE2_TIME, now):
            logger.info("[%s][%s] Phase-2 transition triggered at %02d:%02d",
                        trade.trade_id, STRATEGY_NAME, now.hour, now.minute)
            result = self._execute_phase2(trade, oc, broker, ltps, now)
            if result:
                trade.iss_phase2_done = True
                trade.iss_phase = 2
                return True, None, None   # signal to bot: record update, no single-leg swap
            return False, None, None

        # ── Phase-3 transition at 15:28 ─────────────────────────────
        if (trade.iss_phase2_done and not trade.iss_phase3_done
                and self._is_at_or_after(PHASE3_TIME, now)):
            logger.info("[%s][%s] Phase-3 transition triggered at %02d:%02d",
                        trade.trade_id, STRATEGY_NAME, now.hour, now.minute)
            result = self._execute_phase3(trade, oc, broker, ltps, now)
            if result:
                trade.iss_phase3_done = True
                trade.iss_phase = 3
                return True, None, None
            return False, None, None

        # ── Check SL hit on active short legs ────────────────────────
        self._sync_sl_status(trade, broker)

        return False, None, None

    # ─────────────────────────────────────────────────────────────────
    # PHASE-2: 14:15
    # Close OTM-6 shorts (if not already SL'd), open ATM straddle shorts
    # ─────────────────────────────────────────────────────────────────

    def _execute_phase2(self, trade, oc, broker, ltps, now):
        atm_strike = self._get_atm(oc)
        if atm_strike is None:
            logger.error("[%s][%s] Phase-2: cannot determine ATM from option chain",
                         trade.trade_id, STRATEGY_NAME)
            return False

        # Close OTM-6 CE short if still open
        self._close_short_if_open(trade, broker, ltps, "ce")
        # Close OTM-6 PE short if still open
        self._close_short_if_open(trade, broker, ltps, "pe")

        time.sleep(1)

        # Open ATM CE short with SL
        atm_ce_row = oc[oc["Strike Price"] == atm_strike]
        atm_pe_row = oc[oc["Strike Price"] == atm_strike]

        if atm_ce_row.empty:
            logger.error("[%s][%s] Phase-2: ATM strike %d not in chain",
                         trade.trade_id, STRATEGY_NAME, atm_strike)
            return False

        atm_ce = atm_ce_row.iloc[0]
        atm_pe = atm_pe_row.iloc[0]

        atm_ce_ltp = float(atm_ce["CE LTP"])
        atm_pe_ltp = float(atm_pe["PE LTP"])

        ce_leg, ce_sl_id = self._place_short_with_sl(
            trade, broker,
            symbol     = broker.get_security_name(int(atm_ce["CE SECURITY_ID"])),
            ltp        = atm_ce_ltp,
            option_type= "CE",
            strike     = atm_strike,
            lots       = config.MAX_LOTS_PER_TRADE,
            expiry     = trade.legs[0].expiry if trade.legs else "",
        )
        if ce_leg is None:
            return False

        pe_leg, pe_sl_id = self._place_short_with_sl(
            trade, broker,
            symbol     = broker.get_security_name(int(atm_pe["PE SECURITY_ID"])),
            ltp        = atm_pe_ltp,
            option_type= "PE",
            strike     = atm_strike,
            lots       = config.MAX_LOTS_PER_TRADE,
            expiry     = trade.legs[0].expiry if trade.legs else "",
        )
        if pe_leg is None:
            return False

        trade.iss_short_ce       = ce_leg
        trade.iss_short_ce_sl_id = ce_sl_id
        trade.iss_short_pe       = pe_leg
        trade.iss_short_pe_sl_id = pe_sl_id

        from strategies import OptionLeg
        trade.legs.append(ce_leg)
        trade.legs.append(pe_leg)

        logger.info(
            "[%s][%s] Phase-2 complete: ATM straddle CE %d @ Rs.%.1f | PE %d @ Rs.%.1f",
            trade.trade_id, STRATEGY_NAME,
            atm_strike, atm_ce_ltp, atm_strike, atm_pe_ltp,
        )
        return True

    # ─────────────────────────────────────────────────────────────────
    # PHASE-3: 15:28
    # Close ATM straddle shorts (if not SL'd), sell far-OTM (hedges
    # become new shorts to expire worthless overnight — intraday: just
    # sell and immediately square off before 15:30)
    # ─────────────────────────────────────────────────────────────────

    def _execute_phase3(self, trade, oc, broker, ltps, now):
        # Close ATM CE short if still open
        self._close_short_if_open(trade, broker, ltps, "ce")
        # Close ATM PE short if still open
        self._close_short_if_open(trade, broker, ltps, "pe")

        time.sleep(1)

        # Sell far-OTM CE (same strikes as the hedges bought at 09:16,
        # which are by now deep OTM and should be trading around 1–5)
        hedge_ce = getattr(trade, "iss_hedge_ce", None)
        hedge_pe = getattr(trade, "iss_hedge_pe", None)

        if hedge_ce is not None and hedge_ce.status == "OPEN":
            far_ce_ltp = ltps.get(hedge_ce.symbol, hedge_ce.entry_premium)
            sell_id = broker.place_sell_order(hedge_ce.symbol, hedge_ce.quantity)
            if sell_id:
                fill = broker.get_executed_price(sell_id, paper_ltp=far_ce_ltp)
                hedge_ce.exit_premium = fill
                hedge_ce.status       = "CLOSED"
                logger.info("[%s][%s] Phase-3: sold far-OTM CE %s @ Rs.%.1f",
                            trade.trade_id, STRATEGY_NAME, hedge_ce.symbol, fill)
            time.sleep(1)

        if hedge_pe is not None and hedge_pe.status == "OPEN":
            far_pe_ltp = ltps.get(hedge_pe.symbol, hedge_pe.entry_premium)
            sell_id = broker.place_sell_order(hedge_pe.symbol, hedge_pe.quantity)
            if sell_id:
                fill = broker.get_executed_price(sell_id, paper_ltp=far_pe_ltp)
                hedge_pe.exit_premium = fill
                hedge_pe.status       = "CLOSED"
                logger.info("[%s][%s] Phase-3: sold far-OTM PE %s @ Rs.%.1f",
                            trade.trade_id, STRATEGY_NAME, hedge_pe.symbol, fill)
            time.sleep(1)

        logger.info("[%s][%s] Phase-3 complete — all legs settled",
                    trade.trade_id, STRATEGY_NAME)
        trade.iss_squared_off = True
        return True

    # ─────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _place_short_with_sl(self, trade, broker, symbol, ltp,
                              option_type, strike, lots, expiry):
        """
        Place a SELL market order, then immediately place a SL-M order.
        SL price = min(50% of entry premium per unit, 2000 / quantity).

        Returns (OptionLeg, sl_order_id) or (None, None) on failure.
        """
        from strategies import OptionLeg

        lot_size = getattr(trade, "_lot_size", config.MAX_LOTS_PER_TRADE)
        qty      = lots * lot_size

        order_id = broker.place_sell_order(symbol, qty)
        if not order_id:
            logger.error("[%s][%s] SELL order failed for %s",
                         trade.trade_id, STRATEGY_NAME, symbol)
            return None, None

        fill = broker.get_executed_price(order_id, paper_ltp=ltp)
        time.sleep(0.5)

        # SL price per unit: min(50% of fill, Rs. 2000 / qty)
        sl_price = min(fill * SL_PCT, SL_MAX_INR / qty)
        sl_trigger = round(fill + sl_price, 1)  # SL-M trigger above entry (short)

        # Place SL-M order via OpenAlgo
        sl_order_id = self._place_sl_order(
            broker, symbol, qty, sl_trigger, trade.trade_id
        )

        leg = OptionLeg(
            symbol        = symbol,
            instrument    = INSTRUMENT,
            exchange      = EXCHANGE,
            expiry        = expiry,
            strike        = strike,
            option_type   = option_type,
            transaction   = "SELL",
            lots          = lots,
            quantity      = qty,
            entry_price   = fill,
            entry_premium = fill * qty,
            order_id      = order_id,
        )

        logger.info(
            "[%s][%s] SELL %s %s @ Rs.%.1f | SL trigger Rs.%.1f | sl_order=%s",
            trade.trade_id, STRATEGY_NAME,
            option_type, symbol, fill, sl_trigger, sl_order_id,
        )
        return leg, sl_order_id

    def _place_sl_order(self, broker, symbol, qty, sl_trigger, trade_id):
        """
        Place a SL-M BUY order via OpenAlgo to protect the short leg.
        Returns the SL order id, or empty string on failure.
        """
        try:
            resp = broker.client.placeorder(
                strategy   = STRATEGY_NAME,
                symbol     = symbol,
                action     = "BUY",
                exchange   = EXCHANGE,
                price_type = "SL-M",
                product    = "MIS",     # MIS = intraday on OpenAlgo / NSE
                quantity   = qty,
                trigger_price = sl_trigger,
            )
            if resp and resp.get("status") == "success":
                sl_id = resp.get("orderid", "")
                logger.info("[%s][%s] SL-M order placed: %s trigger=%.1f",
                            trade_id, STRATEGY_NAME, sl_id, sl_trigger)
                return sl_id
            logger.warning("[%s][%s] SL-M order failed: %s",
                           trade_id, STRATEGY_NAME, resp)
        except Exception as e:
            logger.warning("[%s][%s] SL-M order exception: %s",
                           trade_id, STRATEGY_NAME, e)
        return ""

    def _sync_sl_status(self, trade, broker):
        """
        Check whether a SL order has been triggered (filled) via OpenAlgo
        orderstatus. If so, mark the corresponding leg as CLOSED.
        """
        for attr in ("iss_short_ce_sl_id", "iss_short_pe_sl_id"):
            sl_id = getattr(trade, attr, "")
            if not sl_id:
                continue
            try:
                resp = broker.client.orderstatus(
                    order_id=sl_id, strategy=STRATEGY_NAME
                )
                if resp and resp.get("status") == "success":
                    order_status = resp.get("data", {}).get("status", "").upper()
                    if order_status in ("COMPLETE", "FILLED", "TRADED"):
                        # Find the corresponding leg and mark it closed
                        side = "CE" if attr == "iss_short_ce_sl_id" else "PE"
                        for leg in trade.legs:
                            if (leg.option_type == side
                                    and leg.transaction == "SELL"
                                    and leg.status == "OPEN"):
                                fill = float(
                                    resp.get("data", {}).get("average_price", leg.entry_price)
                                )
                                leg.exit_premium = fill
                                leg.status       = "CLOSED"
                                logger.info(
                                    "[%s][%s] SL triggered: %s %s closed @ Rs.%.1f",
                                    trade.trade_id, STRATEGY_NAME,
                                    side, leg.symbol, fill,
                                )
                                setattr(trade, attr, "")   # clear SL id
                                break
            except Exception as e:
                logger.warning("[%s][%s] SL status check failed for %s: %s",
                               trade.trade_id, STRATEGY_NAME, sl_id, e)

    def _close_short_if_open(self, trade, broker, ltps, side):
        """
        Buy back the active short CE or PE leg if it is still OPEN
        (i.e. SL was not triggered). Cancels the pending SL order first.
        """
        leg_attr = "iss_short_ce" if side == "ce" else "iss_short_pe"
        sl_attr  = "iss_short_ce_sl_id" if side == "ce" else "iss_short_pe_sl_id"

        leg   = getattr(trade, leg_attr, None)
        sl_id = getattr(trade, sl_attr,  "")

        if leg is None or leg.status != "OPEN":
            return

        # Cancel pending SL order before buying back
        if sl_id:
            try:
                broker.client.cancelorder(
                    order_id=sl_id, strategy=STRATEGY_NAME
                )
                logger.info("[%s][%s] Cancelled SL order %s for %s",
                            trade.trade_id, STRATEGY_NAME, sl_id, leg.symbol)
            except Exception as e:
                logger.warning("[%s][%s] Cancel SL failed for %s: %s",
                               trade.trade_id, STRATEGY_NAME, sl_id, e)
            setattr(trade, sl_attr, "")

        # Place BUY market order to close the short
        order_id = broker.place_buy_order(leg.symbol, leg.quantity)
        if order_id:
            fill           = broker.get_executed_price(
                order_id,
                paper_ltp=ltps.get(leg.symbol, leg.entry_price)
            )
            leg.exit_premium = fill
            leg.status       = "CLOSED"
            logger.info("[%s][%s] Closed short %s %s @ Rs.%.1f",
                        trade.trade_id, STRATEGY_NAME,
                        leg.option_type, leg.symbol, fill)
        else:
            logger.error("[%s][%s] Failed to close short %s — manual intervention needed",
                         trade.trade_id, STRATEGY_NAME, leg.symbol)

        time.sleep(0.5)

    def _cache_legs(self, trade):
        """
        After Phase-1 entry, index legs by their role for fast access
        in subsequent cycles. Roles are set in entry_criteria via the
        'role' key in each leg_def dict and stored as a leg attribute
        by options_bot.py.
        """
        for leg in trade.legs:
            role = getattr(leg, "role", "")
            if role == "hedge_ce":
                trade.iss_hedge_ce = leg
            elif role == "hedge_pe":
                trade.iss_hedge_pe = leg
            elif role == "short_ce":
                trade.iss_short_ce = leg
            elif role == "short_pe":
                trade.iss_short_pe = leg

        # SL order ids are attached after entry — initialise to empty
        if not hasattr(trade, "iss_short_ce_sl_id"):
            trade.iss_short_ce_sl_id = ""
        if not hasattr(trade, "iss_short_pe_sl_id"):
            trade.iss_short_pe_sl_id = ""

    def _find_far_otm(self, oc, option_type, atm_strike):
        """
        Scan outward from OTM-10 to find the first strike whose LTP
        falls within FAR_OTM_RANGE (3–8). Returns the option chain row
        or None if not found.
        """
        ltp_col = "{} LTP".format(option_type)
        sid_col = "{} SECURITY_ID".format(option_type)

        for steps in range(10, 25):
            if option_type == "CE":
                strike = atm_strike + steps * NIFTY_STEP
            else:
                strike = atm_strike - steps * NIFTY_STEP

            row = oc[oc["Strike Price"] == strike]
            if row.empty:
                continue

            ltp = float(row.iloc[0][ltp_col])
            if FAR_OTM_RANGE[0] <= ltp <= FAR_OTM_RANGE[1]:
                logger.info("[intradayShortSell] Far-OTM %s: strike=%d LTP=%.1f",
                            option_type, strike, ltp)
                return row.iloc[0]

        return None

    def _get_atm(self, oc):
        """Return the strike closest to the midpoint of the chain."""
        if oc.empty:
            return None
        mid_idx = len(oc) // 2
        return int(oc.iloc[mid_idx]["Strike Price"])

    @staticmethod
    def _is_at(hm_tuple, now):
        """True only during the exact minute of the target time."""
        h, m = hm_tuple
        return now.hour == h and now.minute == m

    @staticmethod
    def _is_at_or_after(hm_tuple, now):
        """True at or after the target time (fires every cycle once reached)."""
        h, m = hm_tuple
        return (now.hour, now.minute) >= (h, m)
