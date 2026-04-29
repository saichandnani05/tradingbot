"""Combine indicators + price action + VIX into an actionable trade idea.

Signal quality goals:
  • Minimum score 5/10 required (was 3) — fewer but higher-quality trades
  • Minimum 2.5 : 1 risk-reward on every signal
  • Time-of-day gate: no new entries in first 15 min or last 30 min of session
  • VIX ceiling: skip if VIX > 25 (options too expensive for long premium)
  • Trend AND momentum must BOTH be positive (prevents single-pillar signals)
  • PCR confirmation when available (contrarian OI sentiment)

Each call on ``build_signal`` returns a dict like::

    {
        "symbol":      "NIFTY",
        "bias":        "BULLISH",
        "confidence":  0.72,
        "reasons":     ["EMA9>EMA21", "Supertrend up", "ORB breakout"],
        "underlying_ltp": 24150.0,
        "entry":       155.0,
        "stop_loss":   116.25,   # 25% of entry
        "target":      252.0,    # entry + 2.5 × risk
        "option_symbol": "NIFTY26APR3024150CE",
        "qty":         75,
    }
"""
from __future__ import annotations

import logging
from datetime import time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import CONFIG

from . import indicators as ind
from . import price_action as pa
from . import volatility as vol

log = logging.getLogger(__name__)

# ── IST timezone ──────────────────────────────────────────────────────────────
_IST = timezone(timedelta(hours=5, minutes=30))

# ── Time-of-day gates (no entries in first 15 min or last 30 min) ─────────────
_NO_ENTRY_BEFORE = _time(9, 30)   # avoid 9:15–9:30 whipsaw
_NO_ENTRY_AFTER  = _time(15, 0)   # avoid 15:00–15:30 expiry gamma squeeze

# ── Maximum VIX for long-option trades ────────────────────────────────────────
_VIX_CEILING = 25.0    # above this, premiums are too expensive to buy

# ── Minimum score thresholds ──────────────────────────────────────────────────
_MIN_SCORE_HIGH    = 5   # strong signal — takes trade
_MIN_SCORE_NEUTRAL = 4   # borderline — shows on dashboard but doesn't auto-suggest


# ── Risk-reward constants ──────────────────────────────────────────────────────
_STOP_LOSS_PCT = 0.25    # stop loss at 25% below entry premium
_MIN_RR        = 2.5     # minimum risk:reward ratio
_TARGET_MULT   = 2.5     # target = entry + _TARGET_MULT × risk


# ============================================================ scoring helpers

def _score_trend(snap: Dict) -> Tuple[int, List[str]]:
    """EMA alignment + Supertrend direction → max ±3."""
    score   = 0
    reasons: List[str] = []

    ema9  = snap.get("ema_9",  0)
    ema21 = snap.get("ema_21", 0)
    ema50 = snap.get("ema_50", 0)
    st    = snap.get("supertrend_dir", 0)

    if ema9 > ema21:
        score += 1
        reasons.append("EMA9 > EMA21")
    else:
        score -= 1
        reasons.append("EMA9 < EMA21")

    if ema21 > ema50:
        score += 1
        reasons.append("EMA21 > EMA50")
    else:
        score -= 1
        reasons.append("EMA21 < EMA50")

    if st == 1:
        score += 1
        reasons.append("Supertrend bullish")
    elif st == -1:
        score -= 1
        reasons.append("Supertrend bearish")

    return score, reasons


def _score_momentum(snap: Dict) -> Tuple[int, List[str]]:
    """RSI + MACD + VWAP → max ±3."""
    score   = 0
    reasons: List[str] = []

    rsi       = snap.get("rsi_14", 50)
    macd_hist = snap.get("macd_hist", 0)
    macd_line = snap.get("macd", 0)
    macd_sig  = snap.get("macd_signal", 0)
    close     = snap.get("close", 0)
    vwap_val  = snap.get("vwap", 0)

    # RSI: 55–75 bullish, 45–25 bearish (avoid extreme overbought/oversold for entries)
    if 55 < rsi < 75:
        score += 1
        reasons.append(f"RSI {rsi:.1f} bullish zone")
    elif 25 < rsi < 45:
        score -= 1
        reasons.append(f"RSI {rsi:.1f} bearish zone")
    elif rsi >= 75:
        # Overbought — reduce score even if bullish
        score += 0
        reasons.append(f"RSI {rsi:.1f} overbought (skip)")
    elif rsi <= 25:
        score += 0
        reasons.append(f"RSI {rsi:.1f} oversold (skip)")

    # MACD histogram positive AND crossing up
    if macd_hist > 0 and macd_line > macd_sig:
        score += 1
        reasons.append("MACD bullish crossover")
    elif macd_hist < 0 and macd_line < macd_sig:
        score -= 1
        reasons.append("MACD bearish crossover")

    # Price vs VWAP
    if vwap_val > 0:
        if close > vwap_val * 1.001:   # above VWAP with a small buffer
            score += 1
            reasons.append("Above VWAP")
        elif close < vwap_val * 0.999:
            score -= 1
            reasons.append("Below VWAP")

    return score, reasons


def _score_price_action(df: pd.DataFrame) -> Tuple[int, List[str]]:
    """ORB breakout + Bollinger squeeze + candle patterns → max ±4."""
    score   = 0
    reasons: List[str] = []
    snap    = ind.snapshot(df) if not df.empty else {}

    # ── Opening Range Breakout (weighted +2/-2) ────────────────────────────
    orb = pa.opening_range(df)
    brk = pa.breakout_signal(df, orb) if orb else None
    if brk == "BULLISH":
        score += 2
        reasons.append("ORB bullish breakout")
    elif brk == "BEARISH":
        score -= 2
        reasons.append("ORB bearish breakdown")

    # ── Bollinger squeeze: price near upper/lower band ─────────────────────
    close    = snap.get("close", 0)
    bb_upper = snap.get("bb_upper", 0)
    bb_lower = snap.get("bb_lower", 0)
    bb_mid   = snap.get("bb_mid", 0)
    if bb_upper and bb_lower and bb_mid:
        band_width = bb_upper - bb_lower
        if band_width > 0:
            pos_in_band = (close - bb_lower) / band_width   # 0=lower, 1=upper
            if pos_in_band > 0.8:
                score += 1
                reasons.append("Near Bollinger upper (momentum)")
            elif pos_in_band < 0.2:
                score -= 1
                reasons.append("Near Bollinger lower (momentum)")

    # ── Candle patterns ────────────────────────────────────────────────────
    flags = pa.candle_flags(df)
    if flags["bullish_engulfing"]:
        score += 1
        reasons.append("Bullish engulfing")
    if flags["bearish_engulfing"]:
        score -= 1
        reasons.append("Bearish engulfing")

    return score, reasons


def _score_pcr(pcr_val: Optional[float]) -> Tuple[int, List[str]]:
    """Put-Call Ratio: contrarian OI signal → max ±1."""
    if pcr_val is None:
        return 0, []
    if pcr_val > 1.2:
        return 1, [f"PCR {pcr_val:.2f} — bearish hedging (contrarian bullish)"]
    if pcr_val < 0.7:
        return -1, [f"PCR {pcr_val:.2f} — call overloading (contrarian bearish)"]
    return 0, [f"PCR {pcr_val:.2f} neutral"]


# ============================================================ gate checkers

def _time_gate() -> Optional[str]:
    """Return a reason string if we should NOT trade right now, else None."""
    now_ist = datetime.now(_IST).time()
    if now_ist < _NO_ENTRY_BEFORE:
        return f"Too early — waiting for 9:30 IST opening (now {now_ist.strftime('%H:%M')})"
    if now_ist >= _NO_ENTRY_AFTER:
        return f"Too late — no new entries after 15:00 IST (now {now_ist.strftime('%H:%M')})"
    return None


def _vix_gate(vix: Optional[float]) -> Optional[str]:
    """Return a reason string if VIX is too high for long options."""
    if vix and vix > _VIX_CEILING:
        return f"VIX {vix:.1f} > {_VIX_CEILING} — options too expensive, sitting out"
    return None


def _alignment_gate(t_score: int, m_score: int) -> Optional[str]:
    """Both trend AND momentum must agree (same sign) for a valid signal."""
    if t_score > 0 and m_score <= 0:
        return "Trend bullish but momentum not confirming"
    if t_score < 0 and m_score >= 0:
        return "Trend bearish but momentum not confirming"
    return None


# ============================================================ main API

def build_signal(
    symbol:        str,
    df:            pd.DataFrame,
    spot_ltp:      float,
    vix:           Optional[float],
    option_chain=None,    # unused in paper mode — kept for API compat
    option_ltp_fn=None,   # unused in paper mode
    pcr_val:       Optional[float] = None,   # from nse_data.NSE.pcr()
) -> Dict:
    """Aggregate all strategies into one trade idea for the given underlying.

    Returns a signal dict.  The ``bias`` field is one of:
      BULLISH / BEARISH  — actionable signal
      NEUTRAL            — not enough confluence
      GATED              — time/VIX/alignment filter blocked the signal
      NO_DATA            — insufficient candle history
    """
    snap = ind.snapshot(df)
    if not snap:
        return {"symbol": symbol, "bias": "NO_DATA",
                "reasons": ["Insufficient candles — need at least 30 bars"]}

    # ── Score all pillars ──────────────────────────────────────────────────────
    t_score, t_reasons = _score_trend(snap)
    m_score, m_reasons = _score_momentum(snap)
    p_score, p_reasons = _score_price_action(df)
    c_score, c_reasons = _score_pcr(pcr_val)

    total      = t_score + m_score + p_score + c_score
    all_reasons = t_reasons + m_reasons + p_reasons + c_reasons

    # ── Determine raw bias ────────────────────────────────────────────────────
    if total >= _MIN_SCORE_HIGH:
        bias = "BULLISH"
    elif total <= -_MIN_SCORE_HIGH:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # ── Confidence: squash |score| / max_possible → 0..1 ─────────────────────
    max_possible = 10   # 3 + 3 + 4 + 1 - 1 (PCR is ±1 but slightly asymmetric)
    confidence   = round(min(abs(total) / max_possible, 1.0), 2)

    vix_info = vol.vix_snapshot(vix)

    base = {
        "symbol":         symbol,
        "bias":           bias,
        "confidence":     confidence,
        "score":          total,
        "reasons":        all_reasons,
        "underlying_ltp": spot_ltp,
        "indicators":     snap,
        "vix":            vix_info,
        "pcr":            pcr_val,
    }

    if bias == "NEUTRAL":
        return base

    # ── Apply gates (only block if we have an actionable bias) ────────────────
    gate_msg = _time_gate() or _vix_gate(vix) or _alignment_gate(t_score, m_score)
    if gate_msg:
        base["bias"]    = "GATED"
        base["reasons"] = [f"⛔ {gate_msg}"] + all_reasons
        return base

    return base


def build_option_params(
    base_signal: Dict,
    entry_premium: float,
    atr_val: float,
    vix_info: Dict,
    symbol: str,
) -> Dict:
    """Compute SL, target, qty from a confirmed signal and live premium.

    Uses fixed-fraction risk model:
      • Stop loss  = entry × (1 - _STOP_LOSS_PCT)  [25% loss cap on premium]
      • Target     = entry + _TARGET_MULT × risk    [2.5 : 1 R:R minimum]
    """
    risk   = entry_premium * _STOP_LOSS_PCT
    sl     = round(entry_premium - risk, 2)
    target = round(entry_premium + _TARGET_MULT * risk, 2)

    # Additional ATR-based floor: don't let target be smaller than 1.5 ATR move
    atr_target_floor = entry_premium + vix_info["sl_atr"] * atr_val * 0.8
    target = max(target, round(atr_target_floor, 2))

    lot  = CONFIG.lot_sizes.get(symbol.upper(), 50)
    lots = max(1, int(round(CONFIG.max_lots_per_order * vix_info["size_mult"])))
    qty  = lot * lots

    return {
        "stop_loss":  sl,
        "target":     target,
        "qty":        qty,
        "lots":       lots,
        "lot_size":   lot,
        "risk_reward": round((target - entry_premium) / max(risk, 0.01), 2),
    }
