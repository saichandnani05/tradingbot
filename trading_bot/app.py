"""Flask dashboard for the paper-trading bot.

Default mode: PAPER.  No Angel One credentials required.  Signals come from
live NSE India data (option chain + chart API); Yahoo Finance is used as a
fallback for historical OHLCV (indicator lookback).

Daily profit target: 8% of starting equity.  Once reached, new entries are
locked for the session.  Positions already open are left to run.

Run with:
    python app.py

Then open the URL printed in the terminal (http://127.0.0.1:5000 by default).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime
from typing import Dict, Optional

from flask import Flask, jsonify, render_template, request, send_file
from flask.json.provider import DefaultJSONProvider

import data_source
import nse_data
import options_calc
from analysis import signal_engine
from config import CONFIG
from paper_trader import TRADER


# ================================================================ JSON fix
class NaNSafeProvider(DefaultJSONProvider):
    """Replace NaN / ±Inf with null so the browser can parse the JSON."""

    def dumps(self, obj, **kwargs):
        def _clean(o):
            if isinstance(o, float) and not math.isfinite(o):
                return None
            if isinstance(o, dict):
                return {k: _clean(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [_clean(v) for v in o]
            return o
        return super().dumps(_clean(obj), **kwargs)


# ================================================================ logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
log = logging.getLogger("bot")

# ================================================================ app
app = Flask(__name__)
app.json_provider_class = NaNSafeProvider
app.json = NaNSafeProvider(app)
app.secret_key = CONFIG.flask_secret

# ── Startup: expire positions that were opened on a previous calendar day ─────
_stale = TRADER.expire_stale_positions()
if _stale:
    log.info("Expired %d stale position(s) from a previous session.", _stale)

# ── Daily profit target ───────────────────────────────────────────────────────
DAILY_TARGET_PCT = 8.0   # lock new trades once wallet returns 8 % on the day

# ── Shared in-memory state ───────────────────────────────────────────────────
STATE: Dict = {
    "signals":      {},
    "premiums":     {},
    "vix":          None,
    "last_refresh": None,
    "mode":         "PAPER" if CONFIG.paper_mode else "LIVE",
    "error":        None,
    "auto_closed":  [],
    "market":       {"open": False, "message": "Checking..."},
    "daily":        {
        "target_pct":   DAILY_TARGET_PCT,
        "current_pct":  0.0,
        "locked":       False,
        "start_equity": TRADER.wallet.equity,
    },
}
STATE_LOCK = threading.Lock()

# Maximum candle age (seconds) accepted when market is open
_MAX_CANDLE_AGE_SECS = 25 * 60


# ================================================================ signal logic

def compute_signal(symbol: str, vix: Optional[float]) -> Dict:
    df = data_source.candles(symbol, interval="5m", days=5)
    if df.empty:
        return {"symbol": symbol, "bias": "NO_DATA",
                "reasons": ["yfinance returned empty — market closed or rate-limited"]}

    # ── Candle-freshness check ───────────────────────────────────────────────
    age_secs = data_source.candle_age_seconds(df)
    if data_source.is_market_open() and age_secs > _MAX_CANDLE_AGE_SECS:
        age_min = int(age_secs / 60)
        return {
            "symbol": symbol, "bias": "STALE_DATA",
            "reasons": [
                f"Last candle is {age_min} min old — Yahoo may be rate-limiting.",
                "Signals paused until fresh data arrives.",
            ],
        }

    # ── Live spot price (NSE -> Yahoo fallback) ──────────────────────────────
    live_spot = data_source.spot_ltp(symbol)
    if live_spot and live_spot > 0:
        spot     = live_spot
        spot_src = "NSE_LIVE"
    else:
        spot     = float(df["close"].iloc[-1])
        spot_src = "YAHOO_DELAYED"

    # ── PCR from option chain (OI-based sentiment filter) ────────────────────
    pcr_val: Optional[float] = None
    try:
        pcr_val = nse_data.NSE.pcr(symbol)
    except Exception:
        pass

    # ── Build base signal ────────────────────────────────────────────────────
    base = signal_engine.build_signal(
        symbol=symbol, df=df, spot_ltp=spot, vix=vix,
        option_chain=None, option_ltp_fn=None,
        pcr_val=pcr_val,
    )

    if base.get("bias") not in ("BULLISH", "BEARISH"):
        return base

    # ── Pick ATM option — prefer live NSE LTP, fall back to Black-Scholes ─────
    kind = "CE" if base["bias"] == "BULLISH" else "PE"

    nse_ltp, nse_sym, nse_strike = nse_data.NSE.nearest_atm_ltp(symbol, spot, kind)

    if nse_ltp and nse_ltp > 0:
        opt          = options_calc.pick_option(symbol, spot, kind, override_strike=nse_strike)
        prem         = nse_ltp
        price_source = "NSE_LIVE"
        log.info("NSE live LTP for %s %s: Rs %.2f", nse_sym or "?", kind, prem)
    else:
        opt          = options_calc.pick_option(symbol, spot, kind)
        prem         = options_calc.premium(opt, spot, vix)
        price_source = "BLACK_SCHOLES"
        log.info("BS-estimated premium for %s %s: Rs %.2f", opt.tradingsymbol, kind, prem)

    # ── Compute SL / target with fixed-fraction model (25% risk, 2.5:1 R:R) ──
    snap    = base.get("indicators", {})
    atr_val = snap.get("atr_14", 0) or 0
    params  = signal_engine.build_option_params(
        base_signal=base,
        entry_premium=prem,
        atr_val=atr_val,
        vix_info=base["vix"],
        symbol=symbol,
    )

    final_sym = nse_sym or opt.tradingsymbol

    base.update({
        "option_type":     kind,
        "option_symbol":   final_sym,
        "option_strike":   nse_strike or opt.strike,
        "option_expiry":   opt.expiry.isoformat(),
        "option_exchange": "NFO",
        "option_token":    "",
        "entry":           prem,
        "stop_loss":       params["stop_loss"],
        "target":          params["target"],
        "qty":             params["qty"],
        "lots":            params["lots"],
        "lot_size":        params["lot_size"],
        "risk_reward":     params["risk_reward"],
        "action":          "BUY",
        "price_source":    price_source,
        "spot_source":     spot_src,
    })
    return base


# ================================================================ daily P&L tracker

def _update_daily_target(wallet_snapshot: Dict) -> Dict:
    with STATE_LOCK:
        start_eq = STATE["daily"]["start_equity"]

    equity = wallet_snapshot.get("equity", start_eq)
    if start_eq and start_eq > 0:
        current_pct = round(100.0 * (equity - start_eq) / start_eq, 2)
    else:
        current_pct = 0.0

    locked = current_pct >= DAILY_TARGET_PCT

    return {
        "target_pct":   DAILY_TARGET_PCT,
        "current_pct":  current_pct,
        "locked":       locked,
        "start_equity": start_eq,
    }


# ================================================================ refresh loop

def refresh_once() -> None:
    mkt = data_source.market_status()

    if not mkt["open"]:
        with STATE_LOCK:
            STATE["market"]       = mkt
            STATE["last_refresh"] = datetime.now().strftime("%H:%M:%S")
            # Reset daily equity baseline for tomorrow (only if not locked today)
            if not STATE["daily"]["locked"]:
                snap = TRADER.snapshot()
                STATE["daily"]["start_equity"] = snap["equity"]
        return

    try:
        vix = data_source.india_vix()
        new_signals: Dict[str, Dict] = {}
        premiums:    Dict[str, float] = {}

        for sym in ["NIFTY", "BANKNIFTY"]:
            try:
                sig = compute_signal(sym, vix)
            except Exception as exc:
                log.exception("Signal compute failed for %s", sym)
                sig = {"symbol": sym, "bias": "ERROR", "error": str(exc)}
            new_signals[sym] = sig
            if sig.get("option_symbol") and sig.get("entry"):
                premiums[sig["option_symbol"]] = sig["entry"]

        # Mark-to-market open positions
        for pos in list(TRADER.wallet.positions):
            prem = _repriced_premium(pos, vix)
            if prem is not None:
                premiums[pos.symbol] = prem

        auto_closed = TRADER.mark_to_market(premiums)
        snap        = TRADER.snapshot()
        daily       = _update_daily_target(snap)

        with STATE_LOCK:
            STATE["signals"]      = new_signals
            STATE["premiums"]     = premiums
            STATE["vix"]          = vix
            STATE["last_refresh"] = datetime.now().strftime("%H:%M:%S")
            STATE["error"]        = None
            STATE["auto_closed"]  = (auto_closed or [])[-20:]
            STATE["market"]       = mkt
            STATE["daily"]        = daily

        if daily["locked"]:
            log.info("Daily profit target %.1f%% reached (current %.2f%%). "
                     "New entries locked.", DAILY_TARGET_PCT, daily["current_pct"])

    except Exception as exc:
        log.exception("refresh_once failed")
        with STATE_LOCK:
            STATE["error"]  = str(exc)
            STATE["market"] = mkt


def _repriced_premium(pos, vix: Optional[float]) -> Optional[float]:
    """Best-effort current premium for an open position."""
    # Try NSE live first
    try:
        live_spot = data_source.spot_ltp(pos.underlying)
        if live_spot:
            ltp = nse_data.NSE.nearest_atm_ltp(
                pos.underlying, live_spot, pos.option_type
            )[0]
            if ltp and ltp > 0:
                return ltp
    except Exception:
        pass

    # Fall back to Black-Scholes
    spot = data_source.spot_ltp(pos.underlying)
    if spot is None:
        return None
    expiry = None
    if getattr(pos, "expiry", ""):
        try:
            expiry = date.fromisoformat(pos.expiry[:10])
        except ValueError:
            expiry = None
    if expiry is None:
        # Use symbol-aware expiry so BANKNIFTY gets Wednesday, not Thursday
        expiry = options_calc.next_weekly_expiry(symbol=pos.underlying)
    days = max((expiry - date.today()).days, 1)
    opt  = options_calc.Option(
        underlying=pos.underlying, option_type=pos.option_type,
        strike=pos.strike, expiry=expiry,
        tradingsymbol=pos.symbol, days_to_expiry=days,
    )
    return options_calc.premium(opt, spot, vix)


def refresh_loop() -> None:
    while True:
        refresh_once()
        sleep_secs = 30 if data_source.is_market_open() else 60
        time.sleep(sleep_secs)


# ================================================================ routes

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    # On Vercel there is no background thread — trigger a refresh on every
    # request, but cap it at 25 s so the Lambda always returns before the
    # 60 s maxDuration limit (avoids the browser hanging on "Connecting…").
    import os
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        _t = threading.Thread(target=refresh_once, daemon=True)
        _t.start()
        _t.join(timeout=25)

    with STATE_LOCK:
        payload = dict(STATE)
    payload["wallet"] = TRADER.snapshot()
    payload["config"] = {
        "paper_mode":         CONFIG.paper_mode,
        "max_lots_per_order": CONFIG.max_lots_per_order,
        "starting_wallet":    CONFIG.starting_wallet,
        "daily_target_pct":   DAILY_TARGET_PCT,
    }
    return jsonify(payload)


@app.route("/api/wallet/set", methods=["POST"])
def api_wallet_set():
    data = request.get_json(force=True) or {}
    try:
        amount = float(data.get("amount", 0))
        if amount <= 0:
            raise ValueError("amount must be > 0")
        TRADER.set_balance(amount)
        with STATE_LOCK:
            STATE["daily"] = {
                "target_pct":  DAILY_TARGET_PCT,
                "current_pct": 0.0,
                "locked":      False,
                "start_equity": amount,
            }
        return jsonify({"status": True, "wallet": TRADER.snapshot()})
    except Exception as exc:
        return jsonify({"status": False, "error": str(exc)}), 400


@app.route("/api/wallet/reset", methods=["POST"])
def api_wallet_reset():
    TRADER.reset()
    snap = TRADER.snapshot()
    with STATE_LOCK:
        STATE["daily"] = {
            "target_pct":  DAILY_TARGET_PCT,
            "current_pct": 0.0,
            "locked":      False,
            "start_equity": snap["equity"],
        }
    return jsonify({"status": True, "wallet": snap})


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(force=True) or {}

    mkt = data_source.market_status()
    if not mkt["open"]:
        return jsonify({
            "status": False,
            "error": f"Market is not open ({mkt['message']}). "
                     "Orders accepted only during NSE hours 09:15-15:30 IST.",
        }), 400

    with STATE_LOCK:
        daily = STATE["daily"]
    if daily.get("locked"):
        return jsonify({
            "status": False,
            "error": (f"Daily profit target of {DAILY_TARGET_PCT}% reached "
                      f"(current {daily['current_pct']:.2f}%). "
                      "No new entries for today. Excellent trading!"),
        }), 400

    try:
        resp = TRADER.place_order(
            symbol=data["symbol"],
            underlying=data.get("underlying", ""),
            option_type=data.get("option_type", ""),
            strike=float(data.get("strike", 0) or 0),
            side=data["side"].upper(),
            qty=int(data["qty"]),
            price=float(data["price"]),
            stop_loss=float(data.get("stop_loss", 0) or 0),
            target=float(data.get("target", 0) or 0),
            token=str(data.get("token", "")),
            exchange=data.get("exchange", "NFO"),
            expiry=str(data.get("expiry", "")),
        )
        with STATE_LOCK:
            STATE["premiums"][data["symbol"]] = float(data["price"])
        return jsonify({"status": True, "response": resp, "wallet": TRADER.snapshot()})
    except Exception as exc:
        log.exception("Paper order failed")
        return jsonify({"status": False, "error": str(exc)}), 400


@app.route("/api/close", methods=["POST"])
def api_close():
    data   = request.get_json(force=True) or {}
    pid    = data.get("position_id")
    symbol = data.get("symbol", "")
    try:
        with STATE_LOCK:
            ltp = STATE["premiums"].get(symbol)
        if ltp is None or ltp <= 0:
            client_px = data.get("price")
            if client_px is not None:
                try:
                    ltp = float(client_px)
                except (TypeError, ValueError):
                    ltp = None
        if ltp is None or ltp <= 0:
            pos = next((p for p in TRADER.wallet.positions if p.id == pid), None)
            if pos is not None:
                with STATE_LOCK:
                    vix = STATE.get("vix")
                ltp = _repriced_premium(pos, vix)
                if ltp is None or ltp <= 0:
                    ltp = pos.entry_price
        if ltp is None or ltp <= 0:
            raise ValueError("no live price available for close")
        resp = TRADER.close_position(pid, price=float(ltp), reason="manual")
        return jsonify({"status": True, "response": resp, "wallet": TRADER.snapshot()})
    except Exception as exc:
        return jsonify({"status": False, "error": str(exc)}), 400


@app.route("/api/trades.xlsx")
def api_trades_xlsx():
    from paper_trader import TRADES_FILE
    if not TRADES_FILE.exists():
        return jsonify({"status": False, "error": "no trades yet"}), 404
    return send_file(TRADES_FILE, as_attachment=True, download_name="trades.xlsx")


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    refresh_once()
    return jsonify({"status": True})


@app.route("/api/health")
def api_health():
    return jsonify({
        "mode":                "PAPER" if CONFIG.paper_mode else "LIVE",
        "configured_for_live": CONFIG.is_configured_for_live(),
        "market":              data_source.market_status(),
        "daily_target_pct":    DAILY_TARGET_PCT,
    })


# ================================================================ main

def start_background() -> None:
    """Start the background refresh loop.

    On Vercel (serverless) there are no persistent threads — each request is
    handled by a fresh Lambda invocation.  We detect this and skip the thread;
    instead, /api/state triggers a fresh refresh on every call.
    """
    import os
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        log.info("Vercel detected — background thread skipped; refresh is on-demand.")
        return
    t = threading.Thread(target=refresh_loop, name="signal-refresh", daemon=True)
    t.start()


# ── Auto-start on gunicorn / module import (Vercel / Railway / Render) ────────
# When run by a WSGI server (not __main__), we still want the background thread.
import os as _os
if not _os.getenv("VERCEL") and not _os.getenv("VERCEL_ENV"):
    _bg = threading.Thread(target=refresh_loop, name="signal-refresh", daemon=True)
    _bg.start()


if __name__ == "__main__":
    banner_url = f"http://{CONFIG.flask_host}:{CONFIG.flask_port}"
    print("\n" + "=" * 60)
    print(f"  Trading bot starting in {'PAPER' if CONFIG.paper_mode else 'LIVE'} mode")
    print(f"  Daily profit target: {DAILY_TARGET_PCT}%")
    print(f"  Open in your browser:  {banner_url}")
    print("  Leave this terminal running; close it to stop the bot.")
    print("=" * 60 + "\n")
    app.run(host=CONFIG.flask_host, port=CONFIG.flask_port,
            debug=False, use_reloader=False)
