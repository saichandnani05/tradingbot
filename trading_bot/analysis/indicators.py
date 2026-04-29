"""Technical indicators used by the signal engine.

All functions take a pandas DataFrame with columns [open, high, low, close, volume]
and return the latest scalar value or a Series, depending on the function.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- moving avgs
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


# ---------------------------------------------------------------- RSI
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------- MACD
def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


# ---------------------------------------------------------------- VWAP
def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets at the start of each calendar day.

    VWAP must accumulate from 09:15 of the *current* session, not across
    multiple days.  When volume is 0 or 1 (e.g. NSE chart data without real
    volume), falls back to a cumulative average of the typical price.
    """
    if df.empty:
        return pd.Series(dtype=float)

    result = pd.Series(index=df.index, dtype=float)

    for day, day_df in df.groupby(df.index.date):
        tp    = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
        vol   = day_df["volume"]
        total_vol = vol.sum()

        if total_vol > len(day_df):
            # Real volume — standard VWAP
            cum_v  = vol.cumsum().replace(0, np.nan)
            result.loc[day_df.index] = (tp * vol).cumsum() / cum_v
        else:
            # Unit / zero volume (NSE chart data) — use running avg of typical price
            result.loc[day_df.index] = tp.expanding().mean()

    return result


# ---------------------------------------------------------------- Bollinger
def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(series, window)
    std = series.rolling(window).std()
    return pd.DataFrame({
        "mid": mid,
        "upper": mid + num_std * std,
        "lower": mid - num_std * std,
    })


# ---------------------------------------------------------------- ATR
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ---------------------------------------------------------------- Supertrend
def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_ = atr(df, period)
    upper = hl2 + multiplier * atr_
    lower = hl2 - multiplier * atr_

    final_upper = upper.copy()
    final_lower = lower.copy()
    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    for i in range(len(df)):
        if i == 0:
            st.iloc[i] = upper.iloc[i]
            direction.iloc[i] = 1
            continue
        # carry-forward bands
        if df["close"].iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = max(upper.iloc[i], final_upper.iloc[i - 1])
        if df["close"].iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = min(lower.iloc[i], final_lower.iloc[i - 1])

        if st.iloc[i - 1] == final_upper.iloc[i - 1]:
            st.iloc[i] = (
                final_upper.iloc[i]
                if df["close"].iloc[i] <= final_upper.iloc[i]
                else final_lower.iloc[i]
            )
        else:
            st.iloc[i] = (
                final_lower.iloc[i]
                if df["close"].iloc[i] >= final_lower.iloc[i]
                else final_upper.iloc[i]
            )
        direction.iloc[i] = 1 if df["close"].iloc[i] > st.iloc[i] else -1

    return pd.DataFrame({"supertrend": st, "direction": direction})


# ---------------------------------------------------------------- summariser
def snapshot(df: pd.DataFrame) -> Dict[str, float]:
    """Return a dict of the most recent indicator values for dashboard display."""
    if df.empty or len(df) < 30:
        return {}

    close = df["close"]
    macd_df = macd(close)
    bb = bollinger(close)
    st_df = supertrend(df)
    atr_val = atr(df).iloc[-1]

    return {
        "close": float(close.iloc[-1]),
        "ema_9": float(ema(close, 9).iloc[-1]),
        "ema_21": float(ema(close, 21).iloc[-1]),
        "ema_50": float(ema(close, 50).iloc[-1]),
        "rsi_14": float(rsi(close).iloc[-1]),
        "macd": float(macd_df["macd"].iloc[-1]),
        "macd_signal": float(macd_df["signal"].iloc[-1]),
        "macd_hist": float(macd_df["hist"].iloc[-1]),
        "vwap": float(vwap(df).iloc[-1]),
        "bb_upper": float(bb["upper"].iloc[-1]),
        "bb_mid": float(bb["mid"].iloc[-1]),
        "bb_lower": float(bb["lower"].iloc[-1]),
        "atr_14": float(atr_val),
        "supertrend": float(st_df["supertrend"].iloc[-1]),
        "supertrend_dir": int(st_df["direction"].iloc[-1]),
    }
