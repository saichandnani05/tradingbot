"""Synthetic option pricing used in paper mode.

In live mode we fetch premium from Angel One's option chain.  In paper mode
we compute an approximate Black-Scholes premium using India VIX as IV and the
nearest weekly expiry.  Good enough for paper P&L tracking; do not use for
real pricing decisions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta


STRIKE_STEP = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
    "FINNIFTY":  50,
}

# ── NSE weekly option expiry weekdays (0=Mon … 6=Sun) ─────────────────────────
# NIFTY 50   → Thursday (3)
# BANKNIFTY  → Wednesday (2)
# FINNIFTY   → Tuesday (1)
_EXPIRY_WEEKDAY = {
    "NIFTY":     3,   # Thursday
    "BANKNIFTY": 2,   # Wednesday
    "FINNIFTY":  1,   # Tuesday
}


@dataclass
class Option:
    underlying: str
    option_type: str          # "CE" or "PE"
    strike: float
    expiry: date
    tradingsymbol: str
    days_to_expiry: int


def next_weekly_expiry(today: date | None = None,
                       symbol: str = "NIFTY") -> date:
    """Return the NEXT valid expiry date for the given index.

    Each NSE index has its own expiry weekday:
      NIFTY     → Thursday
      BANKNIFTY → Wednesday
      FINNIFTY  → Tuesday

    Same-day expiry is ALWAYS skipped — trading an option that expires today
    carries extreme gamma / theta risk and is never appropriate for this bot.
    If today is the expiry weekday we jump straight to next week's contract.
    """
    today = today or date.today()
    target_wd = _EXPIRY_WEEKDAY.get(symbol.upper(), 3)   # default Thursday

    # How many calendar days until the target weekday?
    days_ahead = (target_wd - today.weekday()) % 7

    # days_ahead == 0 means today IS expiry day — always skip to next week.
    if days_ahead == 0:
        days_ahead = 7

    return today + timedelta(days=days_ahead)


def atm_strike(underlying: str, spot: float) -> float:
    step = STRIKE_STEP.get(underlying.upper(), 50)
    return round(spot / step) * step


def _symbol(underlying: str, expiry: date, strike: float, kind: str) -> str:
    # NSE weekly format: NIFTY26APR3024150CE  (YY + MMM + DD + strike + type)
    # Including the day number prevents confusion with same-month dates.
    mmm = expiry.strftime("%b").upper()
    yy  = expiry.strftime("%y")
    day = expiry.day
    return f"{underlying.upper()}{yy}{mmm}{day}{int(strike)}{kind}"


def pick_option(underlying: str, spot: float, option_type: str,
                today: date | None = None,
                override_strike: float | None = None) -> Option:
    """Return an Option dataclass for the given parameters.

    If ``override_strike`` is supplied (e.g. from the live NSE chain) it is
    used as-is; otherwise the nearest ATM strike is computed from ``spot``.
    The expiry weekday is looked up per-symbol so BANKNIFTY gets Wednesday,
    NIFTY gets Thursday, etc.
    """
    today  = today or date.today()
    expiry = next_weekly_expiry(today, symbol=underlying)   # symbol-aware expiry
    strike = override_strike if override_strike else atm_strike(underlying, spot)
    return Option(
        underlying=underlying.upper(),
        option_type=option_type.upper(),
        strike=strike,
        expiry=expiry,
        tradingsymbol=_symbol(underlying, expiry, strike, option_type.upper()),
        days_to_expiry=max((expiry - today).days, 1),
    )


# ------------------------------------------------------------------ Black-Scholes
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, days: int, iv_pct: float,
             option_type: str, rate: float = 0.065) -> float:
    """Simplified Black-Scholes.  iv_pct is annualised IV in percent (e.g. 15 = 15%)."""
    if spot <= 0 or strike <= 0 or days <= 0 or iv_pct <= 0:
        return max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    T = days / 365.0
    sigma = iv_pct / 100.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * T) * _norm_cdf(d2)
    return strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def premium(opt: Option, spot: float, vix: float | None) -> float:
    iv = float(vix) if vix else 15.0
    return round(bs_price(spot, opt.strike, opt.days_to_expiry, iv, opt.option_type), 2)
