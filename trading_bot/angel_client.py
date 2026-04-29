"""Thin wrapper around Angel One's SmartAPI.

Handles:
  * Login (password + TOTP) and session refresh
  * Instrument master download + symbol token lookup
  * LTP, historical candles, and option chain helpers
  * Order placement (MARKET / LIMIT) with stop loss
  * Safety checks against config limits

The library `smartapi-python` is distributed by Angel One on PyPI.  See their
docs at https://smartapi.angelbroking.com/docs for the underlying REST shapes.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pyotp
import requests

try:
    from SmartApi import SmartConnect          # pip install smartapi-python
except ImportError:                             # pragma: no cover
    SmartConnect = None

from config import CONFIG

log = logging.getLogger(__name__)

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPI_ScripMaster.json"
)


@dataclass
class OrderRequest:
    tradingsymbol: str
    symboltoken: str
    exchange: str          # NFO for options, NSE for equity
    transactiontype: str   # BUY / SELL
    ordertype: str         # MARKET / LIMIT / STOPLOSS
    producttype: str       # INTRADAY / CARRYFORWARD (options NRML)
    quantity: int
    price: float = 0.0
    triggerprice: float = 0.0
    squareoff: float = 0.0
    stoploss: float = 0.0
    duration: str = "DAY"
    variety: str = "NORMAL"


class AngelClient:
    """Singleton-ish wrapper; keep one instance per process."""

    _instruments: Optional[pd.DataFrame] = None
    _instruments_fetched_at: Optional[datetime] = None

    def __init__(self) -> None:
        if SmartConnect is None:
            raise RuntimeError(
                "smartapi-python is not installed.  Run `pip install -r requirements.txt`."
            )
        if not CONFIG.is_configured_for_live():
            raise RuntimeError(
                "Angel One credentials missing.  Copy .env.example to .env and fill it in."
            )
        self.sdk = SmartConnect(api_key=CONFIG.api_key)
        self._lock = threading.Lock()
        self._session: Optional[Dict] = None
        self._session_started: Optional[datetime] = None

    # ------------------------------------------------------------------ auth
    def login(self) -> Dict:
        """Authenticate via MPIN + TOTP and cache the session tokens."""
        totp = pyotp.TOTP(CONFIG.totp_secret).now()
        with self._lock:
            resp = self.sdk.generateSession(
                CONFIG.client_code, CONFIG.mpin, totp
            )
            if not resp or not resp.get("status"):
                raise RuntimeError(f"Login failed: {resp}")
            self._session = resp["data"]
            self._session_started = datetime.now()
            log.info("Angel One login OK for client %s", CONFIG.client_code)
            return self._session

    def ensure_session(self) -> None:
        """Re-login every ~7 hours (token expires in 8)."""
        if (
            self._session is None
            or self._session_started is None
            or datetime.now() - self._session_started > timedelta(hours=7)
        ):
            self.login()

    # ------------------------------------------------------------- instruments
    @classmethod
    def instruments(cls) -> pd.DataFrame:
        """Download and cache the full scrip master (~20 MB, refreshes daily)."""
        if (
            cls._instruments is not None
            and cls._instruments_fetched_at is not None
            and datetime.now() - cls._instruments_fetched_at < timedelta(hours=12)
        ):
            return cls._instruments
        log.info("Downloading Angel scrip master …")
        r = requests.get(INSTRUMENT_URL, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        # strike/expiry normalisation
        df["strike"] = pd.to_numeric(df.get("strike"), errors="coerce") / 100.0
        df["expiry_dt"] = pd.to_datetime(df.get("expiry"), errors="coerce")
        cls._instruments = df
        cls._instruments_fetched_at = datetime.now()
        return df

    def find_index_token(self, underlying: str) -> Dict[str, str]:
        """Return the spot-index token for NIFTY / BANKNIFTY / INDIA VIX."""
        df = self.instruments()
        name_map = {
            "NIFTY": "Nifty 50",
            "BANKNIFTY": "Nifty Bank",
            "INDIAVIX": "India VIX",
            "FINNIFTY": "Nifty Fin Service",
        }
        wanted = name_map.get(underlying.upper(), underlying)
        hit = df[(df["name"] == wanted) & (df["exch_seg"] == "NSE")]
        if hit.empty:
            raise LookupError(f"Index {underlying} not found in scrip master")
        row = hit.iloc[0]
        return {"token": str(row["token"]), "symbol": row["symbol"], "exchange": "NSE"}

    def option_chain(
        self, underlying: str, expiry: Optional[str] = None
    ) -> pd.DataFrame:
        """Return CE/PE rows for the given underlying and expiry (YYYY-MM-DD).

        If expiry is None, uses the nearest weekly expiry.
        """
        df = self.instruments()
        mask = (
            (df["name"] == underlying.upper())
            & (df["exch_seg"] == "NFO")
            & (df["instrumenttype"].isin(["OPTIDX", "OPTSTK"]))
        )
        chain = df[mask].copy()
        if chain.empty:
            raise LookupError(f"No option instruments found for {underlying}")
        if expiry:
            chain = chain[chain["expiry_dt"] == pd.to_datetime(expiry)]
        else:
            nearest = chain["expiry_dt"].min()
            chain = chain[chain["expiry_dt"] == nearest]
        return chain.sort_values(["strike", "symbol"]).reset_index(drop=True)

    # ---------------------------------------------------------------- quotes
    def ltp(self, exchange: str, tradingsymbol: str, token: str) -> float:
        self.ensure_session()
        resp = self.sdk.ltpData(exchange, tradingsymbol, token)
        if not resp or not resp.get("status"):
            raise RuntimeError(f"LTP fetch failed: {resp}")
        return float(resp["data"]["ltp"])

    def historical(
        self,
        exchange: str,
        token: str,
        interval: str = "FIVE_MINUTE",
        days: int = 5,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles.  interval in {ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, ONE_HOUR, ONE_DAY}."""
        self.ensure_session()
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self.sdk.getCandleData(params)
        if not resp or not resp.get("status"):
            raise RuntimeError(f"Historical fetch failed: {resp}")
        rows = resp.get("data") or []
        df = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)
        return df

    # ---------------------------------------------------------------- orders
    def _enforce_safety(self, req: OrderRequest, est_price: float) -> None:
        value = est_price * req.quantity
        if value > CONFIG.max_order_value_inr:
            raise ValueError(
                f"Order value ₹{value:,.0f} exceeds MAX_ORDER_VALUE_INR "
                f"(₹{CONFIG.max_order_value_inr:,})"
            )
        # Is this an underlying we allow?
        allowed = CONFIG.allowed_underlyings
        if allowed and not any(u in req.tradingsymbol.upper() for u in allowed):
            raise ValueError(
                f"Symbol {req.tradingsymbol} is not in ALLOWED_UNDERLYINGS={allowed}"
            )
        # Lot-size check for F&O
        for u, lot in CONFIG.lot_sizes.items():
            if u in req.tradingsymbol.upper():
                max_qty = lot * CONFIG.max_lots_per_order
                if req.quantity > max_qty:
                    raise ValueError(
                        f"Qty {req.quantity} > {CONFIG.max_lots_per_order} lots of {u} "
                        f"(= {max_qty})"
                    )
                break

    def place_order(self, req: OrderRequest, est_price: Optional[float] = None) -> Dict:
        self.ensure_session()
        px = est_price or req.price or 0.0
        if px <= 0:
            # Best-effort LTP for the safety check
            try:
                px = self.ltp(req.exchange, req.tradingsymbol, req.symboltoken)
            except Exception:
                px = 0.0
        if px > 0:
            self._enforce_safety(req, px)

        payload = {
            "variety": req.variety,
            "tradingsymbol": req.tradingsymbol,
            "symboltoken": req.symboltoken,
            "transactiontype": req.transactiontype,
            "exchange": req.exchange,
            "ordertype": req.ordertype,
            "producttype": req.producttype,
            "duration": req.duration,
            "price": str(req.price),
            "triggerprice": str(req.triggerprice),
            "squareoff": str(req.squareoff),
            "stoploss": str(req.stoploss),
            "quantity": str(req.quantity),
        }
        log.info("Placing order: %s", json.dumps(payload))
        resp = self.sdk.placeOrder(payload)
        log.info("Order response: %s", resp)
        # SmartAPI returns the order id string directly on success
        return {"status": True, "order_id": resp} if isinstance(resp, str) else resp

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> Dict:
        self.ensure_session()
        return self.sdk.cancelOrder(order_id=order_id, variety=variety)

    def positions(self) -> List[Dict]:
        self.ensure_session()
        resp = self.sdk.position() or {}
        return resp.get("data") or []

    def funds(self) -> Dict:
        self.ensure_session()
        resp = self.sdk.rmsLimit() or {}
        return resp.get("data") or {}


# Module-level singleton for convenience
_client: Optional[AngelClient] = None


def get_client() -> AngelClient:
    global _client
    if _client is None:
        _client = AngelClient()
        _client.login()
    return _client
