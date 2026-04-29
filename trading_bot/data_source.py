"""Free market-data source for paper trading.

Primary: yfinance.  Fallback: direct Yahoo Finance v8 chart API via requests
with a proper User-Agent (yfinance often chokes when Yahoo rotates its crumb /
cookie requirements — the direct endpoint still works as long as we send a
real browser UA).  If both fail the caller just gets an empty frame / None.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone, time as _time
from typing import Dict, Optional

import pandas as pd
import requests

# ── Market-hours helpers ──────────────────────────────────────────────────────
_IST = timezone(timedelta(hours=5, minutes=30))

# NSE regular session: 09:15 – 15:30 IST, Mon–Fri
_MARKET_OPEN  = _time(9, 15)
_MARKET_CLOSE = _time(15, 30)


def is_market_open() -> bool:
    """Return True if NSE is currently in its regular trading session."""
    now = datetime.now(_IST)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now.time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def market_status() -> dict:
    """Return a dict with open flag and a human-readable message."""
    now = datetime.now(_IST)
    open_ = is_market_open()
    if open_:
        closes_at = now.replace(hour=15, minute=30, second=0, microsecond=0)
        mins_left = int((closes_at - now).total_seconds() / 60)
        msg = f"Open · closes in {mins_left} min"
    elif now.weekday() >= 5:
        msg = "Closed (weekend)"
    elif now.time() < _MARKET_OPEN:
        opens_at = now.replace(hour=9, minute=15, second=0, microsecond=0)
        mins_to = int((opens_at - now).total_seconds() / 60)
        msg = f"Pre-market · opens in {mins_to} min"
    else:
        msg = "Closed (after hours)"
    return {"open": open_, "message": msg}


def candle_age_seconds(df: pd.DataFrame) -> float:
    """Seconds elapsed since the last candle's timestamp (uses IST wall clock)."""
    if df.empty:
        return float("inf")
    last_ts = df.index[-1]
    # Convert to naive IST if it still carries tz info
    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is not None:
        last_ts = last_ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return (datetime.now() - pd.Timestamp(last_ts)).total_seconds()

log = logging.getLogger(__name__)

# Yahoo tickers for the Indian indices
TICKERS: Dict[str, str] = {
    "NIFTY":     "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY":  "NIFTY_FIN_SERVICE.NS",
    "INDIAVIX":  "^INDIAVIX",
}

# Realistic desktop browser UA — Yahoo's v8 chart endpoint will 401/429 a
# plain python-requests User-Agent but happily serves this.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
}

_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    """Shared session that keeps cookies between calls (Yahoo sets a
    consent/B cookie on first hit that stops later requests from being
    rate-limited)."""
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        # Prime the cookie jar by hitting finance.yahoo.com once.
        try:
            s.get("https://finance.yahoo.com/", timeout=6)
        except requests.RequestException:
            pass
        _SESSION = s
    return _SESSION


def _yf():
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "yfinance is not installed.  Run `pip install -r requirements.txt`."
        ) from exc
    return yf


# --------------------------------------------------------------- yfinance path
def _fetch_via_yfinance(ticker: str, interval: str, days: int) -> pd.DataFrame:
    """Call yfinance WITHOUT passing a session.

    Current yfinance versions require a ``curl_cffi`` session (for TLS
    fingerprinting) and will refuse a plain ``requests.Session``.  Letting
    yfinance build its own session is the safe default.
    """
    yf = _yf()
    period = f"{max(days, 1)}d"
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"adj close": "adj_close"})
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if not cols:
        return pd.DataFrame()
    df = df[cols].astype(float)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    return df


# --------------------------------------------------------------- direct path
_RANGE_MAP = {
    "1m": "1d", "2m": "5d", "5m": "5d", "15m": "1mo", "30m": "1mo",
    "60m": "3mo", "1h": "3mo", "1d": "1y",
}


def _fetch_via_yahoo_chart(ticker: str, interval: str, days: int) -> pd.DataFrame:
    """Hit query1.finance.yahoo.com/v8/finance/chart/{ticker} directly."""
    # Pick the smallest Yahoo-accepted range that covers `days`.
    range_ = _RANGE_MAP.get(interval, f"{max(days, 1)}d")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": interval, "range": range_, "includePrePost": "false"}
    j = None  # initialise so the post-loop parse never hits UnboundLocalError
    for attempt in range(3):
        try:
            r = _session().get(url, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            j = r.json()
            break
        except (requests.RequestException, ValueError) as exc:
            log.warning(
                "Yahoo chart fetch failed (%s attempt %d): %s",
                ticker, attempt, exc,
            )
            time.sleep(0.5)
    if j is None:
        return pd.DataFrame()
    try:
        res = j["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return pd.DataFrame()
    ts = res.get("timestamp") or []
    q = (res.get("indicators") or {}).get("quote") or [{}]
    quote = q[0] if q else {}
    if not ts or not quote.get("close"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "open":   quote.get("open") or [None] * len(ts),
        "high":   quote.get("high") or [None] * len(ts),
        "low":    quote.get("low") or [None] * len(ts),
        "close":  quote.get("close") or [None] * len(ts),
        "volume": quote.get("volume") or [0] * len(ts),
    }, index=pd.to_datetime(ts, unit="s", utc=True))
    df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df = df.dropna(subset=["close"])
    if df.empty:
        return df
    return df.astype(float)


# --------------------------------------------------------------- Yahoo helper (internal)
def _fetch_yahoo(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Yahoo Finance candles — delayed ~15–20 min.  Internal fallback only."""
    ticker = TICKERS.get(symbol.upper(), symbol)
    try:
        df = _fetch_via_yfinance(ticker, interval, days)
        if not df.empty:
            return df
    except Exception as exc:  # noqa: BLE001
        log.info("yfinance failed for %s: %s", ticker, exc)
    return _fetch_via_yahoo_chart(ticker, interval, days)


# --------------------------------------------------------------- public API
def candles(symbol: str, interval: str = "5m", days: int = 5) -> pd.DataFrame:
    """Return OHLCV candles indexed by IST timestamps.

    Data priority:
      1. NSE India live chart  — today's intraday candles, real-time, no delay.
      2. Yahoo Finance          — delayed ~15-20 min, used for historical lookback
                                  so indicators have enough bars.

    The two sources are merged: NSE provides today's session; Yahoo provides
    the prior-day history needed for EMA-50, ATR-14 etc.  If NSE is unavailable
    (market closed / rate-limited), we fall back to Yahoo-only.
    """
    # ── 1. NSE live intraday (today's session) ───────────────────────────────
    interval_min = int(interval.rstrip("mMhH")) if interval[-1].lower() in ("m", "h") else 5
    if interval[-1].lower() == "h":
        interval_min *= 60

    nse_df = pd.DataFrame()
    if interval_min <= 15:          # NSE chart API is only useful for sub-30-min intervals
        try:
            from nse_data import NSE
            nse_df = NSE.intraday_ohlcv(symbol, interval_min=interval_min)
        except Exception as exc:    # noqa: BLE001
            log.info("NSE intraday_ohlcv failed for %s: %s", symbol, exc)

    # ── 2. Yahoo historical (for indicator lookback) ──────────────────────────
    yahoo_df = _fetch_yahoo(symbol, interval, max(days, 2))

    # ── 3. Merge: Yahoo history + NSE today ───────────────────────────────────
    if not nse_df.empty and not yahoo_df.empty:
        today = pd.Timestamp.now().normalize()
        hist  = yahoo_df[yahoo_df.index < today]        # keep only pre-today Yahoo bars
        merged = pd.concat([hist, nse_df]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        log.debug("Candles for %s: %d Yahoo hist + %d NSE live = %d total",
                  symbol, len(hist), len(nse_df), len(merged))
        return merged

    if not nse_df.empty:
        return nse_df
    return yahoo_df


def spot_ltp(symbol: str) -> Optional[float]:
    """Real-time spot LTP for an index.

    Priority:
      1. NSE India live feed  (real-time, no delay)
      2. yfinance             (15–20 min delayed, fallback)
      3. Yahoo chart API      (last-ditch)
    """
    # ── 1. NSE live ───────────────────────────────────────────────────────────
    try:
        from nse_data import NSE
        px = NSE.spot_price(symbol)
        if px:
            return px
    except Exception as exc:  # noqa: BLE001
        log.info("NSE spot_price failed for %s — falling back to Yahoo: %s", symbol, exc)

    # ── 2. yfinance (delayed) ─────────────────────────────────────────────────
    df = candles(symbol, interval="1m", days=1)
    if df.empty:
        df = candles(symbol, interval="5m", days=2)
    if not df.empty:
        return float(df["close"].iloc[-1])

    # ── 3. Yahoo chart API metadata price ─────────────────────────────────────
    ticker = TICKERS.get(symbol.upper(), symbol)
    try:
        r = _session().get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "5d"},
            timeout=10,
        )
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        px   = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(px) if px else None
    except Exception:  # noqa: BLE001
        return None


def india_vix() -> Optional[float]:
    """Real-time India VIX — NSE primary, Yahoo fallback."""
    try:
        from nse_data import NSE
        px = NSE.spot_price("INDIAVIX")
        if px:
            return px
    except Exception:  # noqa: BLE001
        pass
    return spot_ltp("INDIAVIX")
