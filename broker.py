from __future__ import annotations
import pdb
import time

"""
broker.py
Dhan-Tradehull connector — API calls match reference code (my_robo.py).
"""

import logging
import random
from pathlib import Path
import json
import pandas as pd

logger = logging.getLogger(__name__)

PAUSE_BETWEEN_CALLS = 0.5   # seconds — to avoid hitting rate limits in live mode

# ── Paper trading helpers ───────────────────────────────────────────

def generate_random():
    """Generate a 9-digit random string to use as a fake orderId."""
    return "".join([str(random.randint(0, 9)) for _ in range(9)])


def generate_bool():
    """Return True/False randomly — used to simulate SL/TG hits in paper mode."""
    return int(generate_random()) % 2 == 1


def load_config():
    import config
    config_file = Path(config.CONFIG_FILE)
    if not config_file.exists():
        raise FileNotFoundError("Missing config.json in {}".format(config_file.parent))
    with open(config_file, "r") as f:
        return json.load(f)


def get_tsl_client():
    """Create and return authenticated Tradehull client."""
    from Dhan_Tradehull import Tradehull
    cfg   = load_config()
    creds = cfg["dhan_config"]
    return Tradehull(creds["client_code"], creds["access_token"])


class DhanBroker:
    """
    Wrapper around Tradehull (tsl) using exact API signatures from reference code.
    Pass in an already-authenticated tsl object.
    """

    def __init__(self, tsl):
        self.tsl = tsl
        self._instrument_df = None   # cached instrument master

    # ── Instrument master (cached) ─────────────────────────────────

    def _get_instrument_df(self):
        if self._instrument_df is None:
            self._instrument_df = self.tsl.get_instrument_file()
        return self._instrument_df

    def get_security_name(self, security_id):
        """Convert numeric security ID → tradingSymbol string."""
        df    = self._get_instrument_df()
        match = df[df["SEM_SMST_SECURITY_ID"] == security_id]
        if match.empty:
            raise ValueError("Security ID {} not found in instrument master".format(security_id))
        return match.iloc[-1]["SEM_CUSTOM_SYMBOL"]

    # ── Account ────────────────────────────────────────────────────

    def get_available_balance(self):
        """
        Live: tsl.Dhan.get_fund_limits()
        Paper: returns a fixed simulated balance so the bot can start without auth.
        Note: 'availabelBalance' is a typo in the Dhan API — kept as-is.
        """
        time.sleep(PAUSE_BETWEEN_CALLS)
        response = self.tsl.Dhan.get_fund_limits()
        if response["status"] != "success":
            err = response["remarks"]
            raise ConnectionError("{}: {}".format(err["error_type"], err["error_message"]))
        return float(response["data"]["availabelBalance"])

    def get_live_pnl(self):
        """Live: tsl.get_live_pnl(). Paper: returns 0.0 (P&L tracked via trade objects)."""
        time.sleep(PAUSE_BETWEEN_CALLS)
        return float(self.tsl.get_live_pnl())

    def kill_switch(self, state="ON"):
        """Emergency stop. state: 'ON' to activate, 'OFF' to deactivate."""
        logger.critical("KILL SWITCH %s triggered!", state)
        self.tsl.kill_switch(state)

    def cancel_all_orders(self):
        return self.tsl.cancel_all_orders()

    # ── Market data ────────────────────────────────────────────────

    def get_ltp(self, names):
        """
        Returns {symbol_name: ltp_float} for each name in the list.
        Returns None if the library returns a failure response.
        Caller must check for None and skip processing accordingly.
        """
        time.sleep(1)
        result = self.tsl.get_ltp_data(names=names)
        if not result or (isinstance(result, dict) and result.get("status") == "failure"):
            logger.warning("get_ltp returned failure response for %s — skipping", names)
            return None
        return result

    def get_lot_size_from_chain(self, underlying, oc):
        """
        Derive lot size from a symbol found in the option chain.
        Picks the first available CE symbol from the chain and calls get_lot_size().
        """
        ce_ids = oc["CE SECURITY_ID"].dropna()
        if ce_ids.empty:
            raise ValueError("No CE entries in option chain for {}".format(underlying))
        first_id = int(ce_ids.iloc[0])
        time.sleep(PAUSE_BETWEEN_CALLS)
        symbol   = self.get_security_name(first_id)
        time.sleep(PAUSE_BETWEEN_CALLS)
        return int(self.tsl.get_lot_size(tradingsymbol=symbol))

    def get_historical_data(self, tradingsymbol, exchange, timeframe):
        """
        exchange:  'INDEX' for NIFTY/BANKNIFTY spot | 'NFO' for F&O
        timeframe: '1' | '5' | '15' | '60' | 'DAY'
        """
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.tsl.get_historical_data(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            timeframe=timeframe,
        )

    def get_option_chain(self, underlying, exchange="INDEX", expiry=1, num_strikes=40):
        """
        Returns (atm_strike: int, option_chain_df: DataFrame).
        expiry: integer — 1=nearest, 2=next, etc.
        Kwarg is capital 'U': Underlying= (exact library requirement).
        DataFrame columns: Strike Price, CE/PE SECURITY_ID, CE/PE Delta, CE/PE OI, CE/PE LTP
        """
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.tsl.get_option_chain(
            Underlying=underlying,
            exchange=exchange,
            expiry=expiry,
            num_strikes=num_strikes,
        )

    def get_expiry_list(self, underlying, exchange="NFO"):
        """
        Returns list of expiry strings. [0]=current, [1]=next, etc.
        Reference: tsl.get_expiry_list('NIFTY', 'NFO')[1]
        """
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.tsl.get_expiry_list(underlying, exchange)

    def get_lot_size(self, tradingsymbol):
        time.sleep(PAUSE_BETWEEN_CALLS)
        return int(self.tsl.get_lot_size(tradingsymbol=tradingsymbol))

    # ── Order placement ────────────────────────────────────────────

    def place_order(self, tradingsymbol, transaction_type, quantity,
                    order_type="MARKET", price=0, trigger_price=0,
                    exchange="NFO", trade_type="CNC"):
        """
        Live: sends order via tsl.order_placement(), returns real orderId.
        Paper: returns a random 9-digit fake orderId, no order sent.
        """
        import config
        if config.PAPER_TRADING:
            fake_id = generate_random()
            logger.info("[PAPER] %s %s qty=%d -> fake orderId=%s",
                        transaction_type, tradingsymbol, quantity, fake_id)
            return fake_id

        logger.info("ORDER -> %s | %s | qty=%d | %s | price=%.1f",
                    transaction_type, tradingsymbol, quantity, order_type, price)
        time.sleep(PAUSE_BETWEEN_CALLS)
        order_id = self.tsl.order_placement(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            order_type=order_type,
            transaction_type=transaction_type,
            trade_type=trade_type,
        )
        logger.info("Placed -> orderId: %s", order_id)
        return order_id

    def place_sell_order(self, tradingsymbol, quantity,
                         order_type="MARKET", price=0, exchange="NFO"):
        """Convenience wrapper: sell (write) an option."""
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.place_order(tradingsymbol, "SELL", quantity,
                                order_type=order_type, price=price, exchange=exchange)

    def place_buy_order(self, tradingsymbol, quantity,
                        order_type="MARKET", price=0, exchange="NFO"):
        """Convenience wrapper: buy back (close) a short option."""
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.place_order(tradingsymbol, "BUY", quantity,
                                order_type=order_type, price=price, exchange=exchange)

    def get_executed_price(self, order_id, paper_ltp=None):
        """
        Live: tsl.get_executed_price(orderid=...) — kwarg is 'orderid', no underscore.
        Paper: returns paper_ltp (the LTP we already have from the option chain),
               so fill price = the last known market price at order time.
        """
        import config
        if config.PAPER_TRADING:
            fill = paper_ltp if paper_ltp is not None else 0.0
            logger.info("[PAPER] Simulated fill for orderId=%s: Rs.%.2f", order_id, fill)
            return fill
        time.sleep(PAUSE_BETWEEN_CALLS)
        return float(self.tsl.get_executed_price(orderid=order_id))

    # ── Positions / Orders ─────────────────────────────────────────

    def get_positions(self):
        """Returns DataFrame of open/CF positions, filters out CLOSED."""
        time.sleep(PAUSE_BETWEEN_CALLS)
        df = self.tsl.get_positions()
        df = df[df["positionType"] != "CLOSED"].copy()
        df["P&L"] = df["unrealizedProfit"] - df["carryForwardSellValue"]
        return df

    def get_orderbook(self):
        time.sleep(PAUSE_BETWEEN_CALLS)
        return self.tsl.get_orderbook()