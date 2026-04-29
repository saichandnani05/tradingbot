"""Paper-trading wallet + position tracker + Excel trade log.

State is persisted to two files in the project folder:

  * wallet.json   — balance, equity, open positions, realized P&L
  * trades.xlsx   — append-only log of every fill (entry + exit = 2 rows)

The wallet supports:

  * set_balance(amount)    — reset starting capital (also zeroes positions)
  * buy / sell             — open a long/short position in an option
  * close(position_id)     — close a specific open position at current LTP
  * mark_to_market(prices) — refresh unrealized P&L for all open positions

Slippage of 0.1% is applied to every fill to loosely simulate spread/impact.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from openpyxl import Workbook, load_workbook

log = logging.getLogger(__name__)

# On Vercel the filesystem is read-only except /tmp; use /tmp there.
_VERCEL   = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))
BASE_DIR  = Path("/tmp") if _VERCEL else Path(__file__).resolve().parent
WALLET_FILE = BASE_DIR / "wallet.json"
TRADES_FILE = BASE_DIR / "trades.xlsx"
SLIPPAGE_PCT = 0.001                  # 0.1% per fill
BROKERAGE_PER_ORDER = 20              # flat ₹20/order (Angel One discount plan)

EXCEL_HEADER = [
    "Trade ID", "Timestamp", "Event", "Symbol", "Underlying", "Option Type",
    "Strike", "Side", "Qty", "Fill Price", "Brokerage", "Realized P&L",
    "Wallet Balance After", "Reason",
]


@dataclass
class Position:
    id: str
    symbol: str
    underlying: str
    option_type: str             # CE / PE
    strike: float
    side: str                    # LONG (bought) / SHORT (sold)
    qty: int                     # in shares (lot_size * lots)
    entry_price: float
    entry_time: str
    stop_loss: float = 0.0
    target: float = 0.0
    ltp: float = 0.0
    unrealized_pnl: float = 0.0
    token: str = ""
    exchange: str = "NFO"
    status: str = "OPEN"          # OPEN / CLOSED
    exit_price: float = 0.0
    exit_time: str = ""
    realized_pnl: float = 0.0
    close_reason: str = ""
    expiry: str = ""              # ISO date (YYYY-MM-DD); used to value MTM correctly


@dataclass
class Wallet:
    starting_balance: float = 100_000.0
    balance: float = 100_000.0      # available cash (after debit for buys)
    realized_pnl: float = 0.0
    positions: List[Position] = field(default_factory=list)
    history: List[Position] = field(default_factory=list)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)

    @property
    def equity(self) -> float:
        """cash + market value of open positions (longs add, shorts subtract).

        ``balance`` already reflects the cash outflow/inflow from opening the
        position, so equity is simply cash plus the current (signed) market
        value of each holding — NOT cash plus P&L.
        """
        signed_mv = sum(
            p.ltp * p.qty * (1 if p.side == "LONG" else -1)
            for p in self.positions
        )
        return round(self.balance + signed_mv, 2)

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)


class PaperTrader:
    """Thread-safe paper broker."""

    def __init__(self) -> None:
        # RLock because mark_to_market holds the lock and then calls
        # close_position which also wants it (same thread re-entry).
        self._lock = threading.RLock()
        self.wallet = self._load()
        self._ensure_excel()

    # ------------------------------------------------------------------ I/O
    @staticmethod
    def _coerce_position(p: Dict) -> "Position":
        """Build a Position from a raw dict, ignoring unknown keys and
        filling in defaults for fields the on-disk row is missing.  Keeps
        wallet.json forward- and backward-compatible across schema tweaks."""
        allowed = {f.name for f in Position.__dataclass_fields__.values()}
        clean = {k: v for k, v in p.items() if k in allowed}
        # Fill required fields with safe defaults if the row is truncated
        required_defaults = {
            "id": "", "symbol": "", "underlying": "", "option_type": "",
            "strike": 0.0, "side": "LONG", "qty": 0,
            "entry_price": 0.0, "entry_time": "",
        }
        for k, v in required_defaults.items():
            clean.setdefault(k, v)
        return Position(**clean)

    def _load(self) -> Wallet:
        if WALLET_FILE.exists():
            try:
                raw = json.loads(WALLET_FILE.read_text())
                positions = [self._coerce_position(p) for p in raw.get("positions", [])]
                history = [self._coerce_position(p) for p in raw.get("history", [])]
                return Wallet(
                    starting_balance=raw.get("starting_balance", 100_000.0),
                    balance=raw.get("balance", 100_000.0),
                    realized_pnl=raw.get("realized_pnl", 0.0),
                    positions=positions,
                    history=history,
                )
            except Exception as exc:
                log.warning("wallet.json is corrupt (%s) — starting fresh", exc)
        return Wallet()

    def _save(self) -> None:
        WALLET_FILE.write_text(json.dumps({
            "starting_balance": self.wallet.starting_balance,
            "balance": self.wallet.balance,
            "realized_pnl": self.wallet.realized_pnl,
            "positions": [asdict(p) for p in self.wallet.positions],
            "history": [asdict(p) for p in self.wallet.history],
        }, indent=2))

    def _ensure_excel(self) -> None:
        if TRADES_FILE.exists():
            return
        wb = Workbook()
        ws = wb.active
        ws.title = "Trades"
        ws.append(EXCEL_HEADER)
        # Bold header
        for cell in ws[1]:
            cell.font = cell.font.copy(bold=True)
        wb.save(TRADES_FILE)

    def _log_to_excel(self, row: List) -> None:
        try:
            wb = load_workbook(TRADES_FILE)
            ws = wb["Trades"] if "Trades" in wb.sheetnames else wb.active
            ws.append(row)
            wb.save(TRADES_FILE)
        except Exception as exc:
            log.exception("Failed to append to trades.xlsx: %s", exc)

    # ---------------------------------------------------------- wallet ops
    def set_balance(self, amount: float) -> Wallet:
        """Reset wallet to a new starting balance (wipes positions + history)."""
        with self._lock:
            self.wallet = Wallet(starting_balance=float(amount), balance=float(amount))
            self._save()
            # Fresh trade log for the new run.  Some filesystems (e.g. network
            # mounts) forbid unlink but allow overwrite — fall back to that.
            try:
                if TRADES_FILE.exists():
                    TRADES_FILE.unlink()
            except OSError as exc:
                log.warning("Could not delete %s (%s); will overwrite.", TRADES_FILE, exc)
                try:
                    wb = Workbook()
                    ws = wb.active
                    ws.title = "Trades"
                    ws.append(EXCEL_HEADER)
                    wb.save(TRADES_FILE)
                    log.info("Overwrote %s instead of deleting.", TRADES_FILE)
                    return self.wallet
                except Exception as exc2:
                    log.warning("Could not overwrite %s either: %s", TRADES_FILE, exc2)
            self._ensure_excel()
            log.info("Wallet reset to ₹%s", amount)
            return self.wallet

    def reset(self) -> Wallet:
        return self.set_balance(self.wallet.starting_balance)

    # ---------------------------------------------------------- trade ops
    @staticmethod
    def _apply_slippage(price: float, side: str) -> float:
        # Buys fill slightly higher, sells slightly lower
        return round(price * (1 + SLIPPAGE_PCT) if side == "BUY" else price * (1 - SLIPPAGE_PCT), 2)

    def place_order(
        self,
        symbol: str,
        underlying: str,
        option_type: str,
        strike: float,
        side: str,                  # "BUY" or "SELL" (closing side of SHORT positions)
        qty: int,
        price: float,
        stop_loss: float = 0.0,
        target: float = 0.0,
        token: str = "",
        exchange: str = "NFO",
        expiry: str = "",
    ) -> Dict:
        """Paper order.  BUY opens a long, SELL opens a short."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        if price <= 0:
            raise ValueError("price must be positive in paper mode")

        with self._lock:
            fill = self._apply_slippage(price, side)
            brokerage = BROKERAGE_PER_ORDER
            now = datetime.now().isoformat(timespec="seconds")

            if side == "BUY":
                cost = fill * qty + brokerage
                if cost > self.wallet.balance:
                    raise ValueError(
                        f"Insufficient funds: need ₹{cost:,.0f}, have ₹{self.wallet.balance:,.0f}"
                    )
                self.wallet.balance -= cost
                pos = Position(
                    id=str(uuid.uuid4())[:8],
                    symbol=symbol, underlying=underlying,
                    option_type=option_type, strike=strike,
                    side="LONG", qty=qty, entry_price=fill, entry_time=now,
                    stop_loss=stop_loss, target=target, ltp=fill,
                    token=token, exchange=exchange, expiry=expiry,
                )
            else:  # SELL — open a short (writing the option)
                # Margin ~= premium received; we credit the premium and hold a
                # short position.  Simplistic but good enough for paper P&L.
                credit = fill * qty - brokerage
                self.wallet.balance += credit
                pos = Position(
                    id=str(uuid.uuid4())[:8],
                    symbol=symbol, underlying=underlying,
                    option_type=option_type, strike=strike,
                    side="SHORT", qty=qty, entry_price=fill, entry_time=now,
                    stop_loss=stop_loss, target=target, ltp=fill,
                    token=token, exchange=exchange, expiry=expiry,
                )

            self.wallet.positions.append(pos)
            self._save()

            self._log_to_excel([
                pos.id, now, "OPEN", symbol, underlying, option_type, strike,
                side, qty, fill, brokerage, 0.0, round(self.wallet.balance, 2),
                "manual",
            ])

            return {"status": True, "position": asdict(pos)}

    def close_position(self, position_id: str, price: float,
                       reason: str = "manual") -> Dict:
        with self._lock:
            for i, pos in enumerate(self.wallet.positions):
                if pos.id != position_id or pos.status != "OPEN":
                    continue
                exit_side = "SELL" if pos.side == "LONG" else "BUY"
                fill = self._apply_slippage(price, exit_side)
                brokerage = BROKERAGE_PER_ORDER
                now = datetime.now().isoformat(timespec="seconds")

                if pos.side == "LONG":
                    proceeds = fill * pos.qty - brokerage
                    self.wallet.balance += proceeds
                    pnl = (fill - pos.entry_price) * pos.qty - 2 * brokerage
                else:  # SHORT — buy back
                    cost = fill * pos.qty + brokerage
                    self.wallet.balance -= cost
                    pnl = (pos.entry_price - fill) * pos.qty - 2 * brokerage

                pos.exit_price = fill
                pos.exit_time = now
                pos.realized_pnl = round(pnl, 2)
                pos.status = "CLOSED"
                pos.close_reason = reason
                self.wallet.realized_pnl += pos.realized_pnl
                self.wallet.history.append(pos)
                self.wallet.positions.pop(i)
                self._save()

                self._log_to_excel([
                    pos.id, now, "CLOSE", pos.symbol, pos.underlying,
                    pos.option_type, pos.strike, exit_side, pos.qty, fill,
                    brokerage, pos.realized_pnl,
                    round(self.wallet.balance, 2), reason,
                ])
                return {"status": True, "position": asdict(pos)}
            raise LookupError(f"No open position with id={position_id}")

    # ---------------------------------------------------------- mark-to-market
    def mark_to_market(self, prices: Dict[str, float]) -> List[str]:
        """Update LTPs, unrealized P&L, and auto-close on SL/target.

        `prices` maps symbol -> latest premium.  Returns a list of human
        messages for any auto-closures that fired.
        """
        closed_msgs: List[str] = []
        with self._lock:
            # Snapshot the symbol list so we can mutate positions during iteration
            for pos in list(self.wallet.positions):
                ltp = prices.get(pos.symbol)
                if ltp is None:
                    continue
                pos.ltp = round(ltp, 2)
                if pos.side == "LONG":
                    pos.unrealized_pnl = round((ltp - pos.entry_price) * pos.qty, 2)
                    hit_sl = pos.stop_loss and ltp <= pos.stop_loss
                    hit_tgt = pos.target and ltp >= pos.target
                else:
                    pos.unrealized_pnl = round((pos.entry_price - ltp) * pos.qty, 2)
                    hit_sl = pos.stop_loss and ltp >= pos.stop_loss
                    hit_tgt = pos.target and ltp <= pos.target

                reason = "SL_HIT" if hit_sl else "TARGET_HIT" if hit_tgt else None
                if reason:
                    try:
                        self.close_position(pos.id, ltp, reason=reason)
                        closed_msgs.append(f"{pos.symbol} auto-closed: {reason} at {ltp:.2f}")
                    except Exception as exc:
                        log.warning("Auto-close failed for %s: %s", pos.id, exc)
            self._save()
        return closed_msgs

    # ---------------------------------------------------------- stale cleanup
    def expire_stale_positions(self) -> int:
        """Close open positions whose option has actually expired on NSE.

        Logic (in priority order):
          1. If the position carries an ``expiry`` field and that date is in
             the past → option has expired on NSE, close at near-zero value.
          2. If no expiry field is stored and the position is from a PREVIOUS
             calendar day → treat as stale and close flat (we have no live price).

        Returns the number of positions closed.
        """
        today = datetime.now().date()
        expired = 0
        with self._lock:
            for pos in list(self.wallet.positions):
                close_reason: str | None = None
                close_price:  float      = 0.05   # near-zero for expired options

                # ── Check 1: option expiry date has passed ─────────────────
                pos_expiry_str = getattr(pos, "expiry", "") or ""
                if pos_expiry_str:
                    try:
                        pos_expiry = datetime.fromisoformat(
                            pos_expiry_str[:10]
                        ).date()
                        if pos_expiry < today:
                            close_reason = "OPTION_EXPIRED"
                            close_price  = 0.05   # expires worthless or near-zero
                    except (ValueError, AttributeError):
                        pass

                # ── Check 2: no expiry stored, position is from an old day ─
                if close_reason is None:
                    entry_date_str = (getattr(pos, "entry_time", "") or "")[:10]
                    if entry_date_str:
                        try:
                            entry_d = datetime.fromisoformat(entry_date_str).date()
                            if entry_d < today and not pos_expiry_str:
                                # Genuinely stale with no expiry info
                                close_reason = "STALE_NO_EXPIRY"
                                close_price  = pos.entry_price   # flat close
                        except (ValueError, AttributeError):
                            pass

                if close_reason:
                    try:
                        self.close_position(pos.id, close_price, reason=close_reason)
                        expired += 1
                        log.info("Closed position %s (%s) — %s",
                                 pos.id, pos.symbol, close_reason)
                    except Exception as exc:
                        log.warning("Could not expire position %s: %s", pos.id, exc)

        if expired:
            log.info("Expired %d position(s) at startup.", expired)
        return expired

    # ---------------------------------------------------------- view
    def snapshot(self) -> Dict:
        w = self.wallet
        return {
            "starting_balance": w.starting_balance,
            "balance": round(w.balance, 2),
            "equity": w.equity,
            "realized_pnl": round(w.realized_pnl, 2),
            "unrealized_pnl": round(w.unrealized_pnl, 2),
            "total_pnl": w.total_pnl,
            "return_pct": round(
                100 * (w.equity - w.starting_balance) / max(w.starting_balance, 1), 2),
            "open_positions": [asdict(p) for p in w.positions],
            "history": [asdict(p) for p in w.history[-50:]],
        }


# Module-level singleton
TRADER = PaperTrader()
