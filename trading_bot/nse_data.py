"""Real-time NSE India data — option chain + index spot prices.

Uses NSE's public web API with proper session / cookie management.
NSE requires cookies from the landing page before API calls succeed.

Primary use:
  • spot_price(symbol)          → real-time index LTP
  • nearest_atm_ltp(...)        → real option-chain LTP for the NEXT valid expiry
  • option_chain(symbol)        → full chain dict for custom filtering
  • intraday_ohlcv(symbol)      → live 5-min OHLCV for today's session
  • pcr(symbol)                 → Put-Call Ratio from live OI (signal filter)

All calls are thread-safe.  The session auto-renews every 3 minutes.
Multiple fallback UA strings rotate on each renewal to reduce fingerprinting.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import date as _date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

log = logging.getLogger(__name__)

_BASE       = "https://www.nseindia.com"
_COOKIE_TTL = 180        # refresh session every 3 minutes (was 4.5)
_TIMEOUT    = 15         # per-request timeout

# ── Rotate UA strings so NSE doesn't fingerprint a single bot identity ────────
_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
     "Gecko/20100101 Firefox/124.0"),
]

def _make_headers(ua: str) -> dict:
    return {
        "User-Agent":       ua,
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US,en;q=0.9",
        "Accept-Encoding":  "gzip, deflate, br",
        "Connection":       "keep-alive",
        "Referer":          "https://www.nseindia.com/",
        "X-Requested-With": "XMLHttpRequest",
        "DNT":              "1",
        "Sec-Fetch-Dest":   "empty",
        "Sec-Fetch-Mode":   "cors",
        "Sec-Fetch-Site":   "same-origin",
    }

# ── Pages NSE checks for cookies — must visit in this order ──────────────────
_PRIME_URLS = [
    f"{_BASE}/",
    f"{_BASE}/option-chain",
    f"{_BASE}/market-data/live-equity-market",
]

# ── Flexible index-name map ───────────────────────────────────────────────────
_INDEX_TOKENS: Dict[str, List[str]] = {
    "NIFTY":     ["NIFTY 50", "NIFTY50", "S&P CNX NIFTY"],
    "BANKNIFTY": ["NIFTY BANK", "BANK NIFTY", "BANKNIFTY"],
    "FINNIFTY":  ["NIFTY FIN", "FIN SERVICE", "FINNIFTY", "NIFTY FINANCIAL"],
    "INDIAVIX":  ["INDIA VIX", "INDIAVIX"],
}


def _parse_nse_date(s: str) -> Optional[_date]:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            pass
    return None


def _index_matches(nse_name: str, symbol: str) -> bool:
    tokens = _INDEX_TOKENS.get(symbol.upper(), [symbol.upper()])
    nse_up = nse_name.upper()
    return any(t.upper() in nse_up or nse_up in t.upper() for t in tokens)


class NSEClient:
    """Thread-safe NSE web-API client with auto-renewing cookie session."""

    def __init__(self) -> None:
        self._lock              = threading.Lock()
        self._session: Optional[requests.Session] = None
        self._last_refresh: float = 0.0
        self._fail_count: int = 0

    # ── Session management ────────────────────────────────────────────────────
    def _refresh_session(self) -> None:
        ua = random.choice(_USER_AGENTS)
        s  = requests.Session()
        s.headers.update(_make_headers(ua))

        # Prime cookies: visit each NSE page in order with small random delays
        ok = False
        for url in _PRIME_URLS:
            try:
                r = s.get(url, timeout=_TIMEOUT)
                if r.status_code == 200:
                    ok = True
                time.sleep(random.uniform(0.3, 0.8))
            except requests.RequestException as exc:
                log.warning("NSE session prime (%s): %s", url, exc)

        if ok:
            self._session      = s
            self._last_refresh = time.time()
            self._fail_count   = 0
            log.info("NSE session refreshed (UA: %s…)", ua[:40])
        else:
            log.warning("NSE session prime failed — all pages unreachable.")

    def _get_session(self) -> Optional[requests.Session]:
        now = time.time()
        if self._session is None or (now - self._last_refresh) > _COOKIE_TTL:
            self._refresh_session()
        return self._session

    # ── Core HTTP ─────────────────────────────────────────────────────────────
    def _get(self, path: str, retries: int = 3) -> Optional[dict]:
        for attempt in range(retries):
            try:
                with self._lock:
                    session = self._get_session()
                if session is None:
                    log.warning("NSE: no active session, skipping %s", path)
                    return None

                url = f"{_BASE}{path}"
                r   = session.get(url, timeout=_TIMEOUT)

                # NSE returns 401/403 when cookies expire mid-session
                if r.status_code in (401, 403):
                    log.info("NSE %d on %s — forcing session refresh.", r.status_code, path)
                    with self._lock:
                        self._last_refresh = 0.0
                    time.sleep(2.0 + attempt * 1.5)
                    continue

                r.raise_for_status()

                # Detect silent failures: NSE sometimes returns 200 with HTML
                ct = r.headers.get("content-type", "")
                if "json" not in ct and "javascript" not in ct:
                    log.warning("NSE returned non-JSON on %s (ct=%s) — refreshing session", path, ct)
                    with self._lock:
                        self._last_refresh = 0.0
                    time.sleep(1.5)
                    continue

                data = r.json()
                if data is None:
                    log.warning("NSE returned null JSON on %s", path)
                    time.sleep(1.5 * (attempt + 1))
                    continue

                return data

            except (requests.RequestException, ValueError) as exc:
                self._fail_count += 1
                log.warning("NSE GET %s attempt %d/%d: %s", path, attempt + 1, retries, exc)
                time.sleep(1.5 * (attempt + 1))

        log.error("NSE GET %s failed after %d attempts.", path, retries)
        return None

    # ── Public API ────────────────────────────────────────────────────────────
    def option_chain(self, symbol: str) -> Optional[dict]:
        """Full NSE option chain for NIFTY / BANKNIFTY / FINNIFTY."""
        return self._get(f"/api/option-chain-indices?symbol={symbol.upper()}")

    def all_indices(self) -> Optional[dict]:
        return self._get("/api/allIndices")

    def spot_price(self, symbol: str) -> Optional[float]:
        """Real-time spot LTP.

        Tries three sources in order:
          1. allIndices endpoint (fastest, covers all NSE indices)
          2. option-chain underlyingValue (fallback for index futures)
        """
        # ── 1. allIndices ─────────────────────────────────────────────────────
        data = self.all_indices()
        if data:
            for item in data.get("data", []):
                nse_name = item.get("index", "")
                if _index_matches(nse_name, symbol):
                    for fld in ("last", "lastPrice", "ltP", "previousClose"):
                        val = item.get(fld)
                        if val:
                            try:
                                fv = float(val)
                                if fv > 0:
                                    log.debug("NSE spot %s = %.2f (via allIndices.%s)",
                                              symbol, fv, fld)
                                    return fv
                            except (TypeError, ValueError):
                                pass

        # ── 2. option-chain underlyingValue ──────────────────────────────────
        if symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            chain = self.option_chain(symbol)
            if chain:
                uv = (chain.get("records") or {}).get("underlyingValue")
                if uv:
                    try:
                        fv = float(uv)
                        if fv > 0:
                            log.debug("NSE spot %s = %.2f (via option-chain)", symbol, fv)
                            return fv
                    except (TypeError, ValueError):
                        pass

        log.warning("NSE spot_price: could not find %s in allIndices or option-chain", symbol)
        return None

    def nearest_atm_ltp(
        self,
        symbol:       str,
        spot:         float,
        option_type:  str,
        expiry_index: int = 0,
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """Return (ltp, tradingsymbol, strike) for the nearest ATM option.

        Only future expiry dates are considered — expired contracts are
        explicitly skipped so we never trade a stale series.
        """
        data = self.option_chain(symbol)
        if not data:
            log.warning("NSE option chain empty for %s", symbol)
            return None, None, None

        records = data.get("records") or {}

        # ── 1. Filter to FUTURE expiries only ─────────────────────────────────
        today           = _date.today()
        raw_expiries    = records.get("expiryDates") or []
        future_expiries: List[str] = []
        for e in raw_expiries:
            d = _parse_nse_date(e)
            if d and d >= today:
                future_expiries.append(e)

        if not future_expiries:
            log.warning(
                "No future expiry dates found for %s "
                "(chain had %d dates, today=%s, raw=%s)",
                symbol, len(raw_expiries), today,
                raw_expiries[:5] if raw_expiries else "[]",
            )
            return None, None, None

        expiry = future_expiries[min(expiry_index, len(future_expiries) - 1)]
        log.info("%s: using expiry %s (today=%s, %d future expiries available)",
                 symbol, expiry, today, len(future_expiries))

        chain_rows = records.get("data") or []

        # ── 2. Find nearest ATM strike for this expiry ─────────────────────────
        best_row  = None
        best_diff = float("inf")
        for row in chain_rows:
            if row.get("expiryDate") != expiry:
                continue
            strike = float(row.get("strikePrice") or 0)
            if not strike:
                continue
            diff = abs(strike - spot)
            if diff < best_diff:
                best_diff = diff
                best_row  = row

        if best_row is None:
            log.warning("No chain rows found for %s expiry=%s (total rows=%d)",
                        symbol, expiry, len(chain_rows))
            return None, None, None

        # ── 3. Get LTP — walk outward if ATM has no trades ────────────────────
        def _ltp(row: dict) -> Optional[float]:
            d = row.get(option_type) or {}
            for fld in ("lastPrice", "ltp", "ask", "bid"):
                v = d.get(fld)
                if v is not None:
                    try:
                        fv = float(v)
                        if fv > 0:
                            return fv
                    except (TypeError, ValueError):
                        pass
            return None

        strike = float(best_row.get("strikePrice") or 0)
        ltp    = _ltp(best_row)

        if not ltp:
            expiry_rows = sorted(
                [r for r in chain_rows if r.get("expiryDate") == expiry
                 and float(r.get("strikePrice") or 0) > 0],
                key=lambda r: abs(float(r.get("strikePrice", 0) or 0) - spot),
            )
            for row in expiry_rows[:10]:
                ltp = _ltp(row)
                if ltp:
                    strike = float(row.get("strikePrice") or 0)
                    log.debug("Fell back to strike %.0f for %s %s (ATM had no LTP)",
                              strike, symbol, option_type)
                    break

        if not ltp:
            log.warning("No traded LTP found for %s %s expiry=%s near spot=%.2f",
                        symbol, option_type, expiry, spot)
            return None, None, strike

        # ── 4. Build NSE tradingsymbol ────────────────────────────────────────
        # NSE weekly format: NIFTY26APR3024150CE  (YY + MMM + DD + STRIKE + TYPE)
        exp_dt = _parse_nse_date(expiry)
        if exp_dt:
            sym = (f"{symbol.upper()}"
                   f"{exp_dt.strftime('%y')}"
                   f"{exp_dt.strftime('%b').upper()}"
                   f"{exp_dt.day}"
                   f"{int(strike)}"
                   f"{option_type}")
        else:
            sym = f"{symbol.upper()}{int(strike)}{option_type}"

        log.info("NSE live: %s %s → sym=%s ltp=%.2f expiry=%s",
                 symbol, option_type, sym, ltp, expiry)
        return float(ltp), sym, float(strike)

    # ── Put-Call Ratio (OI-based) ─────────────────────────────────────────────
    def pcr(self, symbol: str, expiry_index: int = 0) -> Optional[float]:
        """Put-Call Ratio from current open interest.

        PCR > 1.2 → bearish hedging (contrarian bullish signal)
        PCR < 0.8 → call overloading (contrarian bearish signal)
        PCR 0.8–1.2 → neutral
        """
        data = self.option_chain(symbol)
        if not data:
            return None
        records = data.get("records") or {}
        today   = _date.today()
        raw_exp = records.get("expiryDates") or []
        future  = [e for e in raw_exp if (_parse_nse_date(e) or _date.min) >= today]
        if not future:
            return None
        expiry = future[min(expiry_index, len(future) - 1)]

        call_oi = put_oi = 0.0
        for row in (records.get("data") or []):
            if row.get("expiryDate") != expiry:
                continue
            call_oi += float((row.get("CE") or {}).get("openInterest") or 0)
            put_oi  += float((row.get("PE") or {}).get("openInterest") or 0)

        if call_oi <= 0:
            return None
        ratio = round(put_oi / call_oi, 3)
        log.debug("PCR for %s expiry=%s → %.3f", symbol, expiry, ratio)
        return ratio

    # ── Intraday OHLCV ────────────────────────────────────────────────────────
    def intraday_ohlcv(self, symbol: str, interval_min: int = 5) -> pd.DataFrame:
        """Today's live intraday OHLCV from NSE's chart API."""
        _IDX = {
            "NIFTY":     "NIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY":  "FINNIFTY",
        }
        idx  = _IDX.get(symbol.upper(), symbol.upper())
        data = self._get(f"/api/chart-databyindex?index={idx}&indices=true")
        if not data:
            return pd.DataFrame()

        graph = (data.get("grapthData")
                 or data.get("graphData")
                 or data.get("grapthdata")
                 or [])
        if not graph:
            log.warning("NSE intraday_ohlcv: no graph data for %s", symbol)
            return pd.DataFrame()

        try:
            ts     = pd.to_datetime([r[0] for r in graph], unit="ms", utc=True)
            closes = pd.Series([float(r[1]) for r in graph], index=ts, name="close")
            closes.index = closes.index.tz_convert("Asia/Kolkata").tz_localize(None)
        except Exception as exc:
            log.warning("NSE intraday_ohlcv parse error for %s: %s", symbol, exc)
            return pd.DataFrame()

        today         = pd.Timestamp.now().normalize()
        session_start = today + pd.Timedelta(hours=9,  minutes=15)
        session_end   = today + pd.Timedelta(hours=15, minutes=31)
        closes = closes[(closes.index >= session_start) & (closes.index <= session_end)]
        if closes.empty:
            return pd.DataFrame()

        rule = f"{interval_min}min"
        df   = closes.resample(rule).ohlc()
        df.columns = ["open", "high", "low", "close"]
        df   = df.dropna()
        df["volume"] = 1.0
        return df.astype(float)

    def option_greeks_snapshot(
        self,
        symbol:       str,
        spot:         float,
        option_type:  str,
        strike:       float,
        expiry_index: int = 0,
    ) -> Optional[dict]:
        """Full option-chain row for a specific strike (OI, IV, delta, etc.)."""
        data = self.option_chain(symbol)
        if not data:
            return None
        records      = data.get("records") or {}
        raw_expiries = records.get("expiryDates") or []
        today        = _date.today()
        future       = [e for e in raw_expiries
                        if (_parse_nse_date(e) or _date.min) >= today]
        if not future:
            return None
        expiry = future[min(expiry_index, len(future) - 1)]
        for row in (records.get("data") or []):
            if (row.get("expiryDate") == expiry
                    and float(row.get("strikePrice") or 0) == strike):
                return row.get(option_type)
        return None


# ── Module-level singleton ────────────────────────────────────────────────────
NSE = NSEClient()
