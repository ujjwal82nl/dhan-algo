from __future__ import annotations

"""
Strategy engine — strike selection & entry/exit logic
for NIFTY and BANKNIFTY weekly options selling.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List, Tuple

import config

logger = logging.getLogger(__name__)


@dataclass
class OptionLeg:
    symbol:        str
    instrument:    str          # NIFTY | BANKNIFTY
    exchange:      str          # INDEX
    expiry:        str          # e.g. '25JAN2025'
    strike:        int
    option_type:   str          # CE | PE
    transaction:   str          # SELL | BUY  ← new field
    lots:          int
    quantity:      int          # lots * lot_size
    entry_price:   float        # price per unit at which the leg was executed (positive for SELL, negative for BUY)
    entry_premium: float        # per-unit fill price
    entry_time:    datetime           = field(default_factory=datetime.now)
    exit_premium:  Optional[float]    = None   # per-unit exit price
    exit_time:     Optional[datetime] = None
    order_id:      str                = ""
    status:        str                = "OPEN"   # OPEN | CLOSED | EXPIRED


@dataclass
class Trade:
    trade_id:   str
    instrument: str    # e.g. BANKNIFTY, CRUDEOIL
    exchange:   str    # e.g. INDEX, MCX  — sourced from config.INSTRUMENTS
    strategy:   str
    legs:       List[OptionLeg] = field(default_factory=list)
    status:     str             = "OPEN"
    entry_date: date            = field(default_factory=date.today)

    @property
    def total_premium_collected(self):
        """
        For SELL legs: premium collected = entry_premium (positive credit)
        For BUY  legs: premium paid      = entry_premium (negative credit)
        Net credit = sum over all legs with sign.
        """
        total = 0.0
        for leg in self.legs:
            val = leg.entry_premium
            total += val if leg.transaction == "SELL" else -val
        return total

    @property
    def current_premium(self):
        """
        Current mark-to-market cost to close all open legs.
        SELL legs: cost to buy back  = exit_premium (or entry) * quantity
        BUY  legs: value received    = exit_premium (or entry) * quantity
        """
        total = 0.0
        for leg in self.legs:
            price = (leg.exit_premium or leg.entry_premium) * leg.quantity
            total += price if leg.transaction == "SELL" else -price
        return total

    @property
    def pnl(self):
        return self.total_premium_collected - self.current_premium

    @property
    def pnl_pct(self):
        if self.total_premium_collected == 0:
            return 0.0
        return self.pnl / self.total_premium_collected * 100


class StrikeSelector:
    """Select strikes based on delta or % OTM from spot."""

    @staticmethod
    def nearest_otm_strike(spot, instrument, option_type, delta_target=None):
        step    = 50 if instrument == "NIFTY" else 100
        otm_pct = 0.025
        raw     = spot * (1 + otm_pct) if option_type == "CE" else spot * (1 - otm_pct)
        return int(round(raw / step) * step)

    @staticmethod
    def select_strike_by_delta(chain, option_type, target_delta):
        delta_col  = "CE Delta" if option_type == "CE" else "PE Delta"
        candidates = chain[chain[delta_col].notna()].copy()
        if candidates.empty:
            return None
        candidates["_delta_dist"] = (candidates[delta_col].abs() - target_delta).abs()
        best = candidates.loc[candidates["_delta_dist"].idxmin()]
        if abs(best[delta_col]) > config.MAX_DELTA:
            logger.warning("Best delta %.2f exceeds MAX_DELTA %.2f",
                           best[delta_col], config.MAX_DELTA)
            return None
        return best


class EntryFilter:
    """Pre-trade checks before placing any order."""

    def __init__(self, open_positions, daily_loss):
        self.open_positions = open_positions
        self.daily_loss     = daily_loss

    def can_enter(self, instrument, premium):
        open_count = len([t for t in self.open_positions if t.status == "OPEN"])
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, "Max open positions ({}) reached".format(config.MAX_OPEN_POSITIONS)

        if self.daily_loss >= config.MAX_DAILY_LOSS_INR:
            return False, "Daily loss limit Rs.{} hit".format(config.MAX_DAILY_LOSS_INR)

        if premium < config.MIN_PREMIUM:
            return False, "Premium {:.0f} below MIN_PREMIUM {}".format(
                premium, config.MIN_PREMIUM)

        already_in = any(
            t.instrument == instrument and t.status == "OPEN"
            for t in self.open_positions
        )
        if already_in:
            return False, "Already have an open position in {}".format(instrument)

        return True, "OK"


class ExitManager:
    """Decide whether an open trade should be closed."""

    @staticmethod
    def should_exit(trade, current_premiums):
        for leg in trade.legs:
            if leg.symbol in current_premiums:
                leg.exit_premium = current_premiums[leg.symbol]

        collected = trade.total_premium_collected
        pnl       = trade.pnl

        profit_target = collected * config.PROFIT_TARGET_PCT
        if pnl >= profit_target:
            return True, "Profit target hit ({:.0f} >= {:.0f})".format(pnl, profit_target)

        stop_loss = collected * config.STOP_LOSS_MULTIPLIER
        if trade.current_premium >= stop_loss:
            return True, "Stop loss hit (current {:.0f} >= SL {:.0f})".format(
                trade.current_premium, stop_loss)

        for leg in trade.legs:
            try:
                expiry_date = datetime.strptime(leg.expiry, "%d%b%Y").date()
                if expiry_date == date.today() and datetime.now().hour >= 15:
                    return True, "Expiry day — closing before 3 PM"
            except ValueError:
                pass

        return False, ""


def build_symbol(instrument, strike, option_type, expiry):
    dt      = datetime.strptime(expiry, "%d%b%Y")
    compact = dt.strftime("%d%b%y").upper()
    return "{}{}{}{}".format(instrument, compact, strike, option_type)


# ── Strategy Registry ──────────────────────────────────────────────

def _build_registry():
    from strategy_shortStrangle        import ShortStrangleStrategy
    from strategy_shortStrangle_Adjust import ShortStrangleAdjustStrategy
    return {
        ShortStrangleStrategy.NAME:       ShortStrangleStrategy,
        ShortStrangleAdjustStrategy.NAME: ShortStrangleAdjustStrategy,
    }


def get_strategy(name):
    registry = _build_registry()
    if name not in registry:
        raise ValueError(
            "Unknown strategy '{}'. Registered: {}".format(name, list(registry.keys()))
        )
    return registry[name]()