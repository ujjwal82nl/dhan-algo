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
    expiry:        str          # e.g. '25JAN2025'
    strike:        int
    option_type:   str          # CE | PE
    lots:          int
    quantity:      int          # lots * lot_size
    entry_premium: float
    entry_time:    datetime = field(default_factory=datetime.now)
    exit_premium:  Optional[float] = None
    exit_time:     Optional[datetime] = None
    order_id:      str = ""
    status:        str = "OPEN"  # OPEN | CLOSED | EXPIRED


@dataclass
class Trade:
    trade_id:    str
    instrument:  str
    strategy:    str            # short_strangle | short_straddle | short_put | short_call
    legs:        List[OptionLeg] = field(default_factory=list)
    status:      str = "OPEN"
    entry_date:  date = field(default_factory=date.today)

    @property
    def total_premium_collected(self):
        return sum(leg.entry_premium * leg.quantity for leg in self.legs)

    @property
    def current_premium(self):
        return sum(
            (leg.exit_premium or leg.entry_premium) * leg.quantity
            for leg in self.legs
        )

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
        """
        Find an OTM strike.
        Falls back to % OTM estimation when live delta is unavailable.
        """
        step = 50 if instrument == "NIFTY" else 100
        otm_pct = 0.025  # ~0.20 delta for weekly
        if option_type == "CE":
            raw_strike = spot * (1 + otm_pct)
        else:
            raw_strike = spot * (1 - otm_pct)
        return int(round(raw_strike / step) * step)

    @staticmethod
    def select_strike_by_delta(chain, option_type, target_delta):
        """
        Pick strike closest to target_delta from option chain DataFrame.
        chain: option chain DataFrame with CE/PE Delta columns.
        """
        delta_col = "CE Delta" if option_type == "CE" else "PE Delta"
        candidates = chain[chain[delta_col].notna()].copy()
        if candidates.empty:
            return None

        candidates["_delta_dist"] = (candidates[delta_col].abs() - target_delta).abs()
        best = candidates.loc[candidates["_delta_dist"].idxmin()]

        if abs(best[delta_col]) > config.MAX_DELTA:
            logger.warning("Best strike delta %.2f exceeds MAX_DELTA %.2f",
                           best[delta_col], config.MAX_DELTA)
            return None
        return best


class EntryFilter:
    """Pre-trade checks before placing any order."""

    def __init__(self, open_positions, daily_loss):
        self.open_positions = open_positions
        self.daily_loss = daily_loss

    def can_enter(self, instrument, premium):
        open_count = len([t for t in self.open_positions if t.status == "OPEN"])

        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, "Max open positions ({}) reached".format(config.MAX_OPEN_POSITIONS)

        if self.daily_loss >= config.MAX_DAILY_LOSS_INR:
            return False, "Daily loss limit Rs.{} hit".format(config.MAX_DAILY_LOSS_INR)

        if premium < config.MIN_PREMIUM:
            return False, "Premium {:.0f} below MIN_PREMIUM {}".format(premium, config.MIN_PREMIUM)

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
        """
        current_premiums: { symbol: current_ltp_float }
        Returns (should_exit: bool, reason: str)
        """
        for leg in trade.legs:
            if leg.symbol in current_premiums:
                leg.exit_premium = current_premiums[leg.symbol]

        collected   = trade.total_premium_collected
        current_val = trade.current_premium
        pnl         = collected - current_val

        profit_target = collected * config.PROFIT_TARGET_PCT
        if pnl >= profit_target:
            return True, "Profit target hit ({:.0f} >= {:.0f})".format(pnl, profit_target)

        stop_loss = collected * config.STOP_LOSS_MULTIPLIER
        if current_val >= stop_loss:
            return True, "Stop loss hit (current {:.0f} >= SL {:.0f})".format(current_val, stop_loss)

        for leg in trade.legs:
            try:
                expiry_date = datetime.strptime(leg.expiry, "%d%b%Y").date()
                if expiry_date == date.today() and datetime.now().hour >= 15:
                    return True, "Expiry day — closing before 3 PM"
            except ValueError:
                pass

        return False, ""


def build_symbol(instrument, strike, option_type, expiry):
    """
    Build NSE F&O trading symbol.
    expiry input: '25JAN2025' -> compact: '25JAN25'
    """
    dt      = datetime.strptime(expiry, "%d%b%Y")
    compact = dt.strftime("%d%b%y").upper()
    return "{}{}{}{}".format(instrument, compact, strike, option_type)


# ── Strategy Registry ──────────────────────────────────────────────
# Maps strategy name string → strategy class.
# Add a new line here every time you create a new strategy file.

def _build_registry():
    from strategy_shortStrangle import ShortStrangleStrategy
    from strategy_shortStrangle_Adjust import ShortStrangleAdjustStrategy
    return {
        ShortStrangleStrategy.NAME:       ShortStrangleStrategy,
        ShortStrangleAdjustStrategy.NAME: ShortStrangleAdjustStrategy,
        # add future strategies here
    }


def get_strategy(name):
    """
    Return an instantiated strategy object by name.
    Raises ValueError if the name is not in the registry.

    Usage:
        strategy = get_strategy("shortStrangle")
        signal   = strategy.entry_criteria(context)
    """
    registry = _build_registry()
    if name not in registry:
        raise ValueError(
            "Unknown strategy '{}'. Registered: {}".format(name, list(registry.keys()))
        )
    return registry[name]()
