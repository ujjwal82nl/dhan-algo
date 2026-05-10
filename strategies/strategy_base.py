from __future__ import annotations

"""
strategy_base.py — Abstract base class for all strategies.

Moved to strategies/ folder.
Imports of top-level modules (config, strategies) work because
options_bot.py adds the project root to sys.path before importing
anything from this package.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class EntrySignal:
    """
    Returned by entry_criteria() when a trade should be opened.

    legs: list of dicts, one per leg to place. Each dict must contain:
      {
          "security_id": int,         # from option chain
          "option_type": "CE" | "PE",
          "transaction": "SELL"|"BUY",
          "ltp":         float,       # last traded price at signal time
          "lots":        int,
      }
    """
    strategy_name: str
    legs: List[Dict[str, Any]] = field(default_factory=list)


class BaseStrategy(ABC):
    """
    Every strategy must subclass this and implement the three methods below.

    The context dict passed by options_bot.py contains:
      instrument    str          e.g. "BANKNIFTY"
      exchange      str          e.g. "INDEX", "MCX"
      atm_strike    int
      expiry        str          e.g. "30MAR2026"
      option_chain  DataFrame    CE/PE delta, OI, LTP, security IDs
      lot_size      int
      open_trades   list[Trade]
      closed_trades list[Trade]
      broker        DhanBroker

    Exit / adjustment context additionally contains:
      trade         Trade
      ltps          {symbol: ltp}
      exit_reason   str          (adjustment_done only)
    """

    NAME: str        = "base"
    DESCRIPTION: str = ""

    @abstractmethod
    def entry_criteria(self, context: dict) -> Optional[EntrySignal]:
        """
        Evaluate market conditions and decide whether to open a new trade.

        Return an EntrySignal to open a trade, or None to skip this cycle.
        """

    @abstractmethod
    def exit_criteria(self, context: dict):
        """
        Decide whether the open trade should be closed.

        Return (True, reason_str) to close, or (False, "") to hold.
        """

    @abstractmethod
    def adjustment_done(self, context: dict) -> bool:
        """
        Called when exit_criteria fires. Implement mid-trade adjustments here.

        Return True if an adjustment was made (bot skips closing this cycle).
        Return False to proceed with a normal full close.
        """
