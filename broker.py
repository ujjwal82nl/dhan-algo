from __future__ import annotations

"""
broker.py — Multi-broker abstraction layer.

Supported modes (set BROKER in config.py):

  "dhan"     — Dhan-Tradehull for everything (data + orders).
               Paper trading works; live orders WIP.

  "openalgo" — OpenAlgo for everything (data + orders).
               Live orders working; delta computed via BS locally.

  "hybrid"   — Tradehull for ALL data calls (option chain with real
               greeks, LTP, expiry list, lot size, positions, orderbook)
               + OpenAlgo for ALL order calls (place, cancel, kill).
               Best of both: real greeks + working live orders.

config.json must contain credentials for the brokers in use:
  dhan_config.client_code / access_token  — for "dhan" or "hybrid"
  openalgo.api_key / host                 — for "openalgo" or "hybrid"

Public interface (all three broker classes implement):
  get_security_name(security_id)
  get_available_balance()
  get_live_pnl()
  get_ltp(names)                          → {symbol: ltp} | None
  get_lot_size_from_chain(underlying, oc) → int
  get_lot_size(tradingsymbol)             → int
  get_option_chain(underlying, exchange, expiry, num_strikes)
                                          → (atm_strike, DataFrame)
  get_expiry_list(underlying, exchange)   → [str]
  get_historical_data(tradingsymbol, exchange, timeframe)
  place_order(tradingsymbol, transaction_type, quantity, ...)
                                          → order_id | None
  place_sell_order(tradingsymbol, quantity, ...)
  place_buy_order(tradingsymbol, quantity, ...)
  get_executed_price(order_id, paper_ltp) → float
  get_positions()                         → DataFrame
  get_orderbook()                         → DataFrame
  cancel_all_orders()
  kill_switch(state)
"""

from logging import config
import time
import logging
import random
import json
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_OA_STRATEGY = "dhan-algo"


# ── Shared helpers ─────────────────────────────────────────────────────────

def generate_random():
    return "".join([str(random.randint(0, 9)) for _ in range(9)])

def generate_bool():
    return int(generate_random()) % 2 == 1

def load_config():
    import config
    cfg_path = Path(config.CONFIG_FILE)
    if not cfg_path.exists():
        raise FileNotFoundError("Missing config.json at {}".format(cfg_path))
    with open(cfg_path, "r") as f:
        return json.load(f)


# ── Client factories ───────────────────────────────────────────────────────

def get_tsl_client():
    from Dhan_Tradehull import Tradehull
    cfg   = load_config()
    creds = cfg["dhan_config"]
    return Tradehull(ClientCode=creds["client_code"], totp_secret=creds["totp_secret"], pin=creds["pin"], mode="pin_totp")

def _get_openalgo_client():
    from openalgo import api
    cfg = load_config()
    oa  = cfg["openalgo"]
    return api(api_key=oa["api_key"], host=oa["host_url"])


# ── Broker factory — the only thing options_bot.py calls ──────────────────

def get_broker():
    """
    Read config.BROKER and return the right broker instance.

    options_bot.py usage (no other change needed):
        from broker import get_broker
        self.broker = get_broker()
    """
    import config
    mode = getattr(config, "BROKER", "dhan").lower()

    if mode == "hybrid":
        tsl    = get_tsl_client()
        client = _get_openalgo_client()
        return HybridBroker(DhanBroker(tsl), OpenAlgoBroker(client))
    elif mode == "openalgo":
        return OpenAlgoBroker(_get_openalgo_client())
    else:
        return DhanBroker(get_tsl_client())


# ══════════════════════════════════════════════════════════════════════════════
# DHAN-TRADEHULL BROKER
# ══════════════════════════════════════════════════════════════════════════════

class DhanBroker:
    """
    Wrapper around Dhan-Tradehull (tsl).
    Paper trading works; live order placement is a known WIP.
    """

    def __init__(self, tsl):
        self.tsl = tsl
        self._instrument_df = None

    # ── Instrument master ───────────────────────────────────────────

    def _get_instrument_df(self):
        if self._instrument_df is None:
            self._instrument_df = self.tsl.get_instrument_file()
        return self._instrument_df

    def get_security_name(self, security_id):
        """Convert numeric security ID → trading symbol string."""
        df    = self._get_instrument_df()
        match = df[df["SEM_SMST_SECURITY_ID"] == security_id]
        if match.empty:
            raise ValueError("Security ID {} not found".format(security_id))
        return match.iloc[-1]["SEM_CUSTOM_SYMBOL"]


    def get_option_symbol(self, security_id):
        """Convert numeric security ID → formatted execution symbol using both columns."""
        df = self._get_instrument_df()
        match = df[df["SEM_SMST_SECURITY_ID"] == security_id]
        
        if match.empty:
            raise ValueError("Security ID {} not found".format(security_id))
            
        # Extract data from the row match
        row = match.iloc[-1]
        custom_symbol = row["SEM_CUSTOM_SYMBOL"]    # 'NIFTY 02 JUN 24300 CALL'
        trading_symbol = row["SEM_TRADING_SYMBOL"]  # 'NIFTY-Jun2026-24300-CE'
        
        # 1. Parse custom symbol for Underlying, Day, and Month
        # ['NIFTY', '02', 'JUN', '24300', 'CALL']
        custom_parts = custom_symbol.split()
        underlying = custom_parts[0]
        expiry_day = custom_parts[1]
        expiry_month = custom_parts[2].upper()
        
        # 2. Parse trading symbol to extract the 2-digit year safely
        # ['NIFTY', 'Jun2026', '24300', 'CE']
        trading_parts = trading_symbol.split('-')
        expiry_year_chunk = trading_parts[1]  # 'Jun2026'
        expiry_year = expiry_year_chunk[-2:]  # Takes the last two characters: '26'
        
        # 3. Extract Strike and Option Type from trading symbol pieces
        strike_price = trading_parts[2]       # '24300'
        option_type = trading_parts[3].upper() # 'CE'
        
        # 4. Assemble into dense format: NIFTY02JUN2624300CE
        return f"{underlying}{expiry_day}{expiry_month}{expiry_year}{strike_price}{option_type}"

    # ── Account ─────────────────────────────────────────────────────

    def get_available_balance(self):
        import config
        if config.PAPER_TRADING:
            return 500000.0
        response = self.tsl.Dhan.get_fund_limits()
        if response["status"] != "success":
            err = response["remarks"]
            raise ConnectionError("{}: {}".format(err["error_type"], err["error_message"]))
        return float(response["data"]["availabelBalance"])   # Dhan API typo — kept as-is

    def get_live_pnl(self):
        import config
        if config.PAPER_TRADING:
            return 0.0
        return float(self.tsl.get_live_pnl())

    def kill_switch(self, state="ON"):
        logger.critical("KILL SWITCH %s triggered!", state)
        self.tsl.kill_switch(state)

    def cancel_all_orders(self):
        return self.tsl.cancel_all_orders()

    # ── Market data ──────────────────────────────────────────────────

    def get_ltp(self, names):
        """Returns {symbol: ltp} or None on failure."""
        time.sleep(1)
        result = self.tsl.get_ltp_data(names=names)
        if not result or (isinstance(result, dict) and result.get("status") == "failure"):
            logger.warning("get_ltp returned failure for %s", names)
            return None
        return result

    def get_lot_size_from_chain(self, underlying, oc):
        ce_ids = oc["CE SECURITY_ID"].dropna()
        if ce_ids.empty:
            raise ValueError("No CE entries in chain for {}".format(underlying))
        symbol = self.get_security_name(int(ce_ids.iloc[0]))
        return int(self.tsl.get_lot_size(tradingsymbol=symbol))

    def get_historical_data(self, tradingsymbol, exchange, timeframe):
        return self.tsl.get_historical_data(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            timeframe=timeframe,
        )

    def get_option_chain(self, underlying, exchange="INDEX", expiry=1, num_strikes=40):
        """
        Returns (atm_strike: int, option_chain_df: DataFrame).
        expiry: integer — 1=nearest, 2=next.
        DataFrame columns: Strike Price, CE/PE SECURITY_ID, CE/PE Delta, CE/PE OI, CE/PE LTP
        """
        return self.tsl.get_option_chain(
            Underlying=underlying,
            exchange=exchange,
            expiry=expiry,
            num_strikes=num_strikes,
        )

    def get_expiry_list(self, underlying, exchange="NFO"):
        """Returns [expiry_str]. [0]=current, [1]=next, etc."""
        return self.tsl.get_expiry_list(underlying, exchange)

    def get_lot_size(self, tradingsymbol):
        return int(self.tsl.get_lot_size(tradingsymbol=tradingsymbol))

    # ── Order placement ──────────────────────────────────────────────

    def place_sell_order(self, strategryName, tradingsymbol, quantity, product="NRML",
                         order_type="MARKET", price=0, exchange="NFO"):
        return self.place_order(tradingsymbol, "SELL", quantity, product=product,
                                order_type=order_type, price=price, exchange=exchange)

    def place_buy_order(self, strategryName, tradingsymbol, quantity, product="NRML",
                        order_type="MARKET", price=0, exchange="NFO"):
        return self.place_order(tradingsymbol, "BUY", quantity, product=product,
                                order_type=order_type, price=price, exchange=exchange)

    def get_executed_price(self, order_id, paper_ltp=None):
        import config
        if config.PAPER_TRADING:
            fill = paper_ltp if paper_ltp is not None else 0.0
            logger.info("[PAPER][Dhan] Simulated fill for %s: Rs.%.2f", order_id, fill)
            return fill
        return float(self.tsl.get_executed_price(orderid=order_id))

    # ── Positions / orders ───────────────────────────────────────────

    def get_positions(self):
        df = self.tsl.get_positions()
        df = df[df["positionType"] != "CLOSED"].copy()
        df["P&L"] = df["unrealizedProfit"] - df["carryForwardSellValue"]
        return df

    def get_orderbook(self):
        return self.tsl.get_orderbook()


# ══════════════════════════════════════════════════════════════════════════════
# OPENALGO BROKER  — orders only
#
# This class handles order placement, execution status, and account actions
# via the OpenAlgo REST API. It has NO data methods (no option chain, no LTP,
# no greeks, no expiry list). All data comes from DhanBroker / Tradehull.
#
# Used directly only in "hybrid" mode (composed inside HybridBroker).
# The "openalgo" standalone mode in get_broker() is kept for completeness
# but will raise NotImplementedError for any data call.
# ══════════════════════════════════════════════════════════════════════════════

class OpenAlgoBroker:
    """
    Order-execution wrapper around the OpenAlgo Python client.

    Handles:
      place_order / place_sell_order / place_buy_order
      get_executed_price
      get_orderbook
      cancel_all_orders
      kill_switch

    Does NOT handle any market data. Use DhanBroker for:
      get_option_chain, get_ltp, get_expiry_list, get_security_name, etc.
    """

    # Exchange string mapping for order placement only
    # Dhan "INDEX" or "NFO" → OpenAlgo "NFO"   (NSE F&O segment)
    # Dhan "MCX"            → OpenAlgo "MCX"
    _ORDER_EXCHANGE = {
        "INDEX": "NFO",
        "NFO":   "NFO",
        "MCX":   "MCX",
    }

    # trade_type → OpenAlgo product code
    _PRODUCT = {
        "CNC":      "CNC",
        "INTRADAY": "MIS",
        "MIS":      "MIS",
        "NRML":     "NRML",
    }

    def __init__(self, client):
        self.client = client   # openalgo.api instance

    # ── Order placement ──────────────────────────────────────────────

    def place_order(self, strategryName, tradingsymbol, transaction_type, quantity, product="NRML",
                    order_type="MARKET", price=0, trigger_price=0,
                    exchange="NFO"):
        """
        Place an order via OpenAlgo.

        order_type  : "MARKET" | "LIMIT" | "SL-M" | "SL"
        transaction_type : "BUY" | "SELL"
        trade_type  : "CNC" | "INTRADAY" | "MIS" | "NRML"
        exchange    : Dhan-style string — translated to OpenAlgo internally.
        """
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
            
        oa_exchange = self._ORDER_EXCHANGE.get(exchange, exchange)
        product     = self._PRODUCT.get(product.upper(), "NRML")

        logger.info("[OpenAlgo] %s %s | qty=%d | %s | exch=%s",
                    transaction_type, tradingsymbol, quantity, order_type, oa_exchange)

        kwargs = dict(
            strategy   = strategryName,
            symbol     = tradingsymbol,
            action     = transaction_type,
            exchange   = oa_exchange,
            price_type = order_type,
            product    = product,
            quantity   = quantity,
        )
        if price and price != 0:
            kwargs["price"] = price
        if trigger_price and trigger_price != 0:
            kwargs["trigger_price"] = trigger_price

        try:

            # resp = self.client.placeorder(
            #     strategy   =  _OA_STRATEGY,
            #     symbol     = tradingsymbol,
            #     action     = transaction_type,
            #     exchange   = oa_exchange,
            #     price_type = order_type, #"MARKET", #
            #     product    = product, #"MIS", 
            #     quantity   = quantity,
            # )

            resp = self.client.placeorder(**kwargs)
            if resp and resp.get("status") == "success":
                order_id = resp.get("orderid", "")
                logger.info("[OpenAlgo] Placed -> orderid: %s", order_id)
                return order_id
            logger.error("[OpenAlgo] placeorder failed: %s", resp)
        except Exception as e:
            logger.error("[OpenAlgo] placeorder exception: %s", e)
        return None

    def place_sell_order(self, strategryName, tradingsymbol, quantity, product="NRML",
                         order_type="MARKET", price=0, exchange="NFO"):
        return self.place_order(strategryName, tradingsymbol, "SELL", quantity, product=product,
                                order_type=order_type, price=price, exchange=exchange)

    def place_buy_order(self, strategryName, tradingsymbol, quantity, product="NRML",
                        order_type="MARKET", price=0, exchange="NFO"):
        return self.place_order(strategryName, tradingsymbol, "BUY", quantity, product=product,
                                order_type=order_type, price=price, exchange=exchange)

    # ── Execution and order status ───────────────────────────────────

    def get_executed_price(self, order_id, paper_ltp=None):
        """
        Fetch the average executed price via orderstatus().
        Falls back to paper_ltp on failure (safe for OpenAlgo analyzer mode).
        """
        import config        
        if config.PAPER_TRADING:
            fill = paper_ltp if paper_ltp is not None else 0.0
            logger.info("[PAPER] Simulated fill for orderId=%s: Rs.%.2f", order_id, fill)
            return fill

        if not order_id:
            return paper_ltp or 0.0
        try:
            resp = self.client.orderstatus(order_id=order_id, strategy=_OA_STRATEGY)
            if resp and resp.get("status") == "success":
                avg = resp.get("data", {}).get("average_price")
                if avg is not None:
                    return float(avg)
        except Exception as e:
            logger.warning("[OpenAlgo] orderstatus() failed for %s: %s", order_id, e)
        fill = paper_ltp if paper_ltp is not None else 0.0
        logger.info("[OpenAlgo] Falling back to paper_ltp=%.2f for order %s", fill, order_id)
        return fill

    def get_orderbook(self):
        """
        Returns orderbook as a DataFrame.
        Column names are normalised to match Tradehull format:
          orderid → orderId,  status → orderStatus
        so strategy SL-status checks work regardless of which broker placed the order.
        """
        try:
            resp = self.client.orderbook()
            if resp and resp.get("status") == "success":
                data   = resp.get("data", {})
                orders = data.get("orders", data) if isinstance(data, dict) else data
                if isinstance(orders, list):
                    df = pd.DataFrame(orders)
                    if "orderid"     in df.columns and "orderId"      not in df.columns:
                        df = df.rename(columns={"orderid":     "orderId"})
                    if "status"      in df.columns and "orderStatus"  not in df.columns:
                        df = df.rename(columns={"status":      "orderStatus"})
                    if "averageprice" in df.columns and "tradedPrice" not in df.columns:
                        df = df.rename(columns={"averageprice": "tradedPrice"})
                    return df
        except Exception as e:
            logger.error("[OpenAlgo] orderbook() failed: %s", e)
        return pd.DataFrame()

    # ── Account actions ──────────────────────────────────────────────

    def cancel_all_orders(self):
        try:
            return self.client.cancelallorder()
        except Exception as e:
            logger.error("[OpenAlgo] cancelallorder() failed: %s", e)

    def kill_switch(self, state="ON"):
        logger.critical("[OpenAlgo] KILL SWITCH — closing all positions")
        try:
            return self.client.closeposition()
        except Exception as e:
            logger.error("[OpenAlgo] closeposition() failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# HYBRID BROKER
# Data  → Dhan-Tradehull  (real greeks, LTP, option chain, positions)
# Orders → OpenAlgo        (live order placement, SL-M, cancel, kill)
# ══════════════════════════════════════════════════════════════════════════════

class HybridBroker:
    """
    Composes DhanBroker (data) and OpenAlgoBroker (orders).

    Every method on this class delegates to one of the two inner brokers.
    The split is:

    ─── DATA (→ DhanBroker / Tradehull) ────────────────────────────────────
      get_security_name        real greeks in the option chain
      get_option_chain         get_ltp
      get_expiry_list          get_lot_size / get_lot_size_from_chain
      get_historical_data      get_positions
      get_orderbook            get_available_balance
      get_live_pnl

    ─── ORDERS (→ OpenAlgoBroker / OpenAlgo) ───────────────────────────────
      place_order              place_sell_order
      place_buy_order          get_executed_price
      cancel_all_orders        kill_switch

    ─── Symbol translation ──────────────────────────────────────────────────
    Tradehull's option chain stores numeric security IDs and returns
    symbol strings via get_security_name().
    OpenAlgo's placeorder takes the broker symbol string directly
    (e.g. "NIFTY02MAY2524000CE").
    get_security_name() is always called before placing an order in
    options_bot.py, so the correct symbol string reaches OpenAlgo.
    """

    def __init__(self, dhan_broker: DhanBroker, oa_broker: OpenAlgoBroker):
        self._data   = dhan_broker     # Tradehull — all data calls
        self._orders = oa_broker       # OpenAlgo  — all order calls
        # Expose inner clients for any strategy code that accesses them directly
        self.tsl     = dhan_broker.tsl
        self.client  = oa_broker.client

    # ── DATA — delegated to Tradehull ─────────────────────────────────

    def get_security_name(self, security_id):
        return self._data.get_security_name(security_id)

    def get_option_symbol(self, security_id):
        return self._data.get_option_symbol(security_id)

    def get_available_balance(self):
        return self._data.get_available_balance()

    def get_live_pnl(self):
        return self._data.get_live_pnl()

    def get_ltp(self, names):
        return self._data.get_ltp(names)

    def get_lot_size_from_chain(self, underlying, oc):
        return self._data.get_lot_size_from_chain(underlying, oc)

    def get_lot_size(self, tradingsymbol):
        return self._data.get_lot_size(tradingsymbol)

    def get_option_chain(self, underlying, exchange="INDEX", expiry=1, num_strikes=40):
        return self._data.get_option_chain(underlying, exchange, expiry, num_strikes)

    def get_expiry_list(self, underlying, exchange="NFO"):
        return self._data.get_expiry_list(underlying, exchange)

    def get_historical_data(self, tradingsymbol, exchange, timeframe):
        return self._data.get_historical_data(tradingsymbol, exchange, timeframe)

    def get_positions(self):
        return self._data.get_positions()

    def get_orderbook(self):
        """
        Returns orderbook from OpenAlgo (order state) with column names
        normalised to match the Tradehull format strategies expect:
          orderId, orderStatus, tradedPrice.
        OpenAlgo orderbook has live order status; Tradehull orderbook
        also works but OpenAlgo is authoritative for orders we placed.
        """
        return self._orders.get_orderbook()

    # ── ORDERS — delegated to OpenAlgo ────────────────────────────────
    def place_sell_order(self, strategryName, tradingsymbol, quantity, product="NRML",
                         order_type="MARKET", price=0, exchange="NFO"):
        return self._orders.place_sell_order(
            strategryName, tradingsymbol, quantity, product=product,
            order_type=order_type, price=price, exchange=exchange,
        )

    def place_buy_order(self, strategryName, tradingsymbol, quantity, product="NRML",  
                        order_type="MARKET", price=0, exchange="NFO"):
        return self._orders.place_buy_order(
            strategryName, tradingsymbol, quantity, product=product,
            order_type=order_type, price=price, exchange=exchange,
        )

    def get_executed_price(self, order_id, paper_ltp=None):
        return self._orders.get_executed_price(order_id, paper_ltp=paper_ltp)

    def cancel_all_orders(self):
        return self._orders.cancel_all_orders()

    def kill_switch(self, state="ON"):
        logger.critical("[Hybrid] KILL SWITCH %s", state)
        return self._orders.kill_switch(state)