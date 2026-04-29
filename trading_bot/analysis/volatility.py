"""Volatility helpers — mostly India VIX regime classification."""
from __future__ import annotations

from typing import Dict, Optional


def vix_regime(vix: float) -> str:
    """Simple 3-bucket classifier used by the signal engine.

    Thresholds are a starting point — tune them from your own backtests.
    """
    if vix < 12.0:
        return "LOW"        # complacent — option premiums cheap, breakouts risky
    if vix < 18.0:
        return "MEDIUM"     # business as usual
    return "HIGH"           # stressed — widen SL or stay out


def position_size_multiplier(vix: float) -> float:
    """Scale qty down when VIX is elevated."""
    if vix >= 25:
        return 0.25
    if vix >= 18:
        return 0.5
    if vix >= 12:
        return 1.0
    return 0.75  # very low VIX — IV crush risk on long options


def sl_atr_multiple(vix: float) -> float:
    """How many ATRs to place stop loss at, given VIX regime."""
    regime = vix_regime(vix)
    return {"LOW": 1.2, "MEDIUM": 1.5, "HIGH": 2.0}[regime]


def vix_snapshot(vix: Optional[float]) -> Dict[str, object]:
    if vix is None:
        return {"vix": None, "regime": "UNKNOWN", "size_mult": 1.0, "sl_atr": 1.5}
    return {
        "vix": float(vix),
        "regime": vix_regime(vix),
        "size_mult": position_size_multiplier(vix),
        "sl_atr": sl_atr_multiple(vix),
    }
