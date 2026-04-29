"""Price-action utilities: opening range, pivots, support/resistance, breakouts."""
from __future__ import annotations

from datetime import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


def opening_range(df: pd.DataFrame, minutes: int = 15) -> Optional[Dict[str, float]]:
    """High/low of the first `minutes` of the current session (9:15–9:30 default).

    Expects df indexed by tz-naive IST timestamps.  Returns None if the session
    hasn't produced enough candles yet.
    """
    if df.empty:
        return None
    last_day = df.index[-1].date()
    today = df[df.index.date == last_day]
    if today.empty:
        return None
    start = pd.Timestamp.combine(last_day, time(9, 15))
    end = start + pd.Timedelta(minutes=minutes)
    window = today[(today.index >= start) & (today.index < end)]
    if window.empty:
        return None
    return {
        "orb_high": float(window["high"].max()),
        "orb_low": float(window["low"].min()),
        "orb_range": float(window["high"].max() - window["low"].min()),
    }


def classical_pivots(prev_day_hlc: Dict[str, float]) -> Dict[str, float]:
    """Classical floor pivots from previous day's H/L/C."""
    h, l, c = prev_day_hlc["high"], prev_day_hlc["low"], prev_day_hlc["close"]
    p = (h + l + c) / 3.0
    return {
        "P": p,
        "R1": 2 * p - l,
        "S1": 2 * p - h,
        "R2": p + (h - l),
        "S2": p - (h - l),
        "R3": h + 2 * (p - l),
        "S3": l - 2 * (h - p),
    }


def prev_day_hlc(df: pd.DataFrame) -> Optional[Dict[str, float]]:
    if df.empty:
        return None
    last_day = df.index[-1].date()
    prior = df[df.index.date < last_day]
    if prior.empty:
        return None
    prev_day = prior.index[-1].date()
    day_df = prior[prior.index.date == prev_day]
    return {
        "high": float(day_df["high"].max()),
        "low": float(day_df["low"].min()),
        "close": float(day_df["close"].iloc[-1]),
    }


def swing_levels(df: pd.DataFrame, lookback: int = 50, touches: int = 2) -> Dict[str, List[float]]:
    """Very lightweight support/resistance: cluster recent highs and lows."""
    if df.empty:
        return {"support": [], "resistance": []}
    recent = df.tail(lookback)
    highs = recent["high"].round(-1).value_counts()
    lows = recent["low"].round(-1).value_counts()
    res = sorted([float(p) for p, n in highs.items() if n >= touches], reverse=True)
    sup = sorted([float(p) for p, n in lows.items() if n >= touches])
    return {"resistance": res[:3], "support": sup[:3]}


def breakout_signal(df: pd.DataFrame, orb: Optional[Dict[str, float]]) -> Optional[str]:
    """Return 'BULLISH', 'BEARISH', or None for opening-range breakout."""
    if not orb or df.empty:
        return None
    last = df["close"].iloc[-1]
    if last > orb["orb_high"]:
        return "BULLISH"
    if last < orb["orb_low"]:
        return "BEARISH"
    return None


# ---------------------------------------------------------------- candles
def is_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return (
        prev["close"] < prev["open"]
        and curr["close"] > curr["open"]
        and curr["close"] > prev["open"]
        and curr["open"] < prev["close"]
    )


def is_bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return (
        prev["close"] > prev["open"]
        and curr["close"] < curr["open"]
        and curr["open"] > prev["close"]
        and curr["close"] < prev["open"]
    )


def is_doji(df: pd.DataFrame, tol: float = 0.1) -> bool:
    if df.empty:
        return False
    row = df.iloc[-1]
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    return rng > 0 and (body / rng) < tol


def candle_flags(df: pd.DataFrame) -> Dict[str, bool]:
    return {
        "bullish_engulfing": is_bullish_engulfing(df),
        "bearish_engulfing": is_bearish_engulfing(df),
        "doji": is_doji(df),
    }
