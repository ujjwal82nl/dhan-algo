from __future__ import annotations

"""
strategy_base.py — Abstract base class for all trading strategies.

Every strategy you create must:
  1. Inherit from BaseStrategy
  2. Implement all three methods: entry_criteria, exit_criteria, adjustment_done
  3. Register itself in the STRATEGY_REGISTRY at the bottom of strategies.py

The bot calls these methods each scan cycle and acts on the returned values.
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Interface that every strategy plugin must implement.

    The bot passes a context dict into each method containing all the
    live market data available at call time:

        context = {
            "instrument":   "NIFTY",          # str
            "atm_strike":   24500,             # int  — ATM from option chain
            "expiry":       "27MAR2025",       # str  — expiry string from Dhan
            "option_chain": <DataFrame>,       # full OC with deltas, OI, LTP
            "lot_size":     50,                # int
            "open_trades":  [...],             # list[Trade] — current open positions
            "closed_trades": [...],            # list[Trade] — closed today
            "broker":       <DhanBroker>,      # broker instance for any extra calls
        }

    For exit_criteria and adjustment_done, the trade being evaluated is
    also passed as "trade" inside the context dict.
    """

    # ── Identity ───────────────────────────────────────────────────
    NAME        = "base"       # override in each subclass — must match STRATEGY_REGISTRY key
    DESCRIPTION = ""           # human-readable description shown at bot startup

    # ── entry_criteria ─────────────────────────────────────────────
    @abstractmethod
    def entry_criteria(self, context):
        """
        Decide whether to enter a trade for this instrument this cycle.

        Returns one of:
            None                    → no trade, skip this instrument
            EntrySignal(...)        → enter a trade with these legs

        Use the context to read option chain, ATM, expiry, etc.
        Do NOT place orders here — just return what you want to trade.
        The bot handles order placement after this returns.
        """
        pass

    # ── exit_criteria ──────────────────────────────────────────────
    @abstractmethod
    def exit_criteria(self, context):
        """
        Decide whether an open trade should be closed this cycle.

        context["trade"] is the Trade object being evaluated.
        context["ltps"]  is {symbol: current_ltp} for all legs.

        Returns:
            (False, "")           → keep the trade open
            (True,  "reason str") → close the trade, reason logged + saved to Excel
        """
        pass

    # ── adjustment_done ────────────────────────────────────────────
    @abstractmethod
    def adjustment_done(self, context):
        """
        Called after exit_criteria returns True, before orders are placed.
        Use this to modify legs, roll strikes, or add hedges.

        Returns:
            False  → no adjustment needed, proceed with normal close
            True   → adjustment was applied (bot skips the normal close)
        """
        pass


# ── EntrySignal — what entry_criteria() returns to the bot ─────────

class EntrySignal:
    """
    Carries the trade setup decided by entry_criteria().
    The bot reads this and places the actual orders.

    legs: list of LegOrder dicts describing each option to trade.
    Each LegOrder:
        {
            "security_id":    int,   # CE SECURITY_ID or PE SECURITY_ID from OC
            "option_type":    str,   # "CE" | "PE"
            "transaction":    str,   # "SELL" | "BUY"
            "lots":           int,
        }
    """

    def __init__(self, legs, strategy_name):
        self.legs          = legs           # list of LegOrder dicts (see above)
        self.strategy_name = strategy_name  # matches BaseStrategy.NAME

    def __repr__(self):
        return "EntrySignal(strategy={}, legs={})".format(self.strategy_name, len(self.legs))
