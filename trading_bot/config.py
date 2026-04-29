"""Configuration loader for the trading bot.

By default the bot runs in PAPER_MODE with a virtual wallet and no Angel One
credentials required.  Flip PAPER_MODE=false in .env (and fill the ANGEL_*
keys) to enable live order placement.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional; env vars still work
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    return val in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    # Mode
    paper_mode: bool = _bool("PAPER_MODE", True)

    # Angel One credentials (only used when paper_mode is False)
    api_key:     str = os.getenv("ANGEL_API_KEY",     "")
    secret_key:  str = os.getenv("ANGEL_SECRET_KEY",  "")   # SmartAPI app secret
    client_code: str = os.getenv("ANGEL_CLIENT_CODE", "")
    mpin:        str = os.getenv("ANGEL_MPIN",        "")
    totp_secret: str = os.getenv("ANGEL_TOTP_SECRET", "")

    # Safety limits (enforced in live mode)
    max_order_value_inr: int = _int("MAX_ORDER_VALUE_INR", 25_000)
    max_lots_per_order: int = _int("MAX_LOTS_PER_ORDER", 2)
    allowed_underlyings: List[str] = field(
        default_factory=lambda: [
            u.strip().upper()
            for u in os.getenv("ALLOWED_UNDERLYINGS", "NIFTY,BANKNIFTY").split(",")
            if u.strip()
        ]
    )

    # Paper wallet defaults
    starting_wallet: float = _float("STARTING_WALLET", 100_000.0)

    # Flask
    flask_secret: str = os.getenv("FLASK_SECRET", "dev-secret-change-me")
    flask_port: int = _int("FLASK_PORT", 5000)
    flask_host: str = os.getenv("FLASK_HOST", "127.0.0.1")

    # NSE lot sizes (update each expiry cycle if the exchange revises them)
    lot_sizes = {
        "NIFTY": 75,
        "BANKNIFTY": 30,
        "FINNIFTY": 65,
    }

    def is_configured_for_live(self) -> bool:
        return all([self.api_key, self.client_code, self.mpin, self.totp_secret])

    def missing_live_fields(self) -> list:
        """Return names of credentials still needed for live mode."""
        missing = []
        if not self.client_code: missing.append("ANGEL_CLIENT_CODE")
        if not self.mpin:        missing.append("ANGEL_MPIN")
        if not self.totp_secret: missing.append("ANGEL_TOTP_SECRET")
        return missing


CONFIG = Config()
