"""Standalone deterministic Paper/Demo broker adapter (zero MT5 or Windows required)."""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from typing import Optional

from x4_cloud.brokers import AccountSnapshot, MarketQuote, OpenPosition, OrderReceipt
from x4_cloud.ledger import utc_now_iso
from x4_cloud.policy import pip_size_for_symbol, pip_value_per_lot_usd

_DEFAULT_ANCHORS: dict[str, tuple[float, float, float]] = {
    # symbol: (base_price, spread_pips, step_volatility_ratio)
    "XAUUSD": (2685.00, 1.8, 0.00045),
    "XAUUSDM": (2685.00, 1.8, 0.00045),
    "EURUSD": (1.08450, 0.9, 0.00022),
    "EURUSDM": (1.08450, 0.9, 0.00022),
    "GBPUSD": (1.27120, 1.2, 0.00026),
    "GBPUSDM": (1.27120, 1.2, 0.00026),
    "USDJPY": (152.400, 1.1, 0.00025),
    "USDJPYM": (152.400, 1.1, 0.00025),
}


class PaperBrokerAdapter:
    """Self-contained live-ticking Paper/Demo broker for cloud runs & testing.

    Simulates realistic bid/ask spreads, mark-to-market floating PnL, automatic
    SL/TP stopouts on every tick, and candle close history for strategy signals.
    """

    mode_name = "paper"

    def __init__(
        self,
        *,
        initial_balance: float = 10000.0,
        symbols: tuple[str, ...] = ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY"),
        seed: Optional[int] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._rng = random.Random(seed)
        self._balance = float(initial_balance)
        self._symbols = tuple(s.upper() for s in symbols)
        self._next_ticket = 400001
        self._positions: dict[int, OpenPosition] = {}
        self._quotes: dict[str, MarketQuote] = {}
        self._price_history: dict[str, deque[float]] = {}
        self._closed_deals: list[dict] = []

        now = time.time()
        for sym in self._symbols:
            base, spread_pips, vol = _DEFAULT_ANCHORS.get(sym, (100.0, 1.5, 0.0003))
            hist: deque[float] = deque(maxlen=250)
            px = base
            # Seed warm-up price series so indicators are ready immediately
            for _ in range(60):
                px = max(pip_size_for_symbol(sym) * 10, px * (1.0 + self._rng.gauss(0.0, vol)))
                hist.append(px)
            self._price_history[sym] = hist
            self._quotes[sym] = self._build_quote(sym, px, spread_pips, now)

    @staticmethod
    def _build_quote(symbol: str, mid: float, spread_pips: float, ts: float) -> MarketQuote:
        pip = pip_size_for_symbol(symbol)
        half = (spread_pips * pip) / 2.0
        digits = 2 if "XAU" in symbol else (3 if "JPY" in symbol else 5)
        bid = round(mid - half, digits)
        ask = round(mid + half, digits)
        return MarketQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread_pips=round(spread_pips, 2),
            ts_epoch=ts,
            age_sec=0.0,
        )

    def get_price_series(self, symbol: str) -> list[float]:
        with self._lock:
            return list(self._price_history.get(symbol.upper(), ()))

    def inject_quote(self, symbol: str, bid: float, ask: float) -> None:
        """Override live quote (used by external feed hooks or deterministic tests)."""
        sym = symbol.upper()
        pip = pip_size_for_symbol(sym)
        spread = round(max(0.0, (ask - bid) / pip), 2)
        now = time.time()
        with self._lock:
            self._quotes[sym] = MarketQuote(sym, bid, ask, spread, now, 0.0)
            hist = self._price_history.setdefault(sym, deque(maxlen=250))
            hist.append((bid + ask) / 2.0)
            self._mark_positions_locked()

    def tick(self) -> None:
        """Advance quotes by one realistic step and evaluate open SL/TP triggers."""
        now = time.time()
        with self._lock:
            for sym in self._symbols:
                prev = self._quotes[sym]
                _, base_spread, vol = _DEFAULT_ANCHORS.get(sym, (100.0, 1.5, 0.0003))
                mid = (prev.bid + prev.ask) / 2.0
                drift = self._rng.gauss(0.0, vol)
                new_mid = max(pip_size_for_symbol(sym) * 10, mid * (1.0 + drift))
                jittered_spread = max(0.3, base_spread * self._rng.uniform(0.85, 1.25))
                self._quotes[sym] = self._build_quote(sym, new_mid, jittered_spread, now)
                self._price_history[sym].append(new_mid)
            self._mark_positions_locked()

    def _mark_positions_locked(self) -> None:
        to_close: list[tuple[int, float, str]] = []
        for tkt, pos in self._positions.items():
            q = self._quotes.get(pos.symbol)
            if not q:
                continue
            mark = q.bid if pos.side == "BUY" else q.ask
            pos.current_price = mark
            pip = pip_size_for_symbol(pos.symbol)
            pips_diff = ((mark - pos.open_price) if pos.side == "BUY" else (pos.open_price - mark)) / pip
            pos.profit = round(pips_diff * pip_value_per_lot_usd(pos.symbol, mark) * pos.volume, 2)

            if pos.side == "BUY":
                if pos.sl > 0 and mark <= pos.sl:
                    to_close.append((tkt, pos.sl, "SL_HIT"))
                elif pos.tp > 0 and mark >= pos.tp:
                    to_close.append((tkt, pos.tp, "TP_HIT"))
            else:
                if pos.sl > 0 and mark >= pos.sl:
                    to_close.append((tkt, pos.sl, "SL_HIT"))
                elif pos.tp > 0 and mark <= pos.tp:
                    to_close.append((tkt, pos.tp, "TP_HIT"))

        for tkt, exit_px, reason in to_close:
            self._close_locked(tkt, exit_px, reason)

    def _close_locked(self, ticket: int, exit_price: float, reason: str) -> Optional[float]:
        pos = self._positions.pop(ticket, None)
        if pos is None:
            return None
        pip = pip_size_for_symbol(pos.symbol)
        pips_diff = ((exit_price - pos.open_price) if pos.side == "BUY" else (pos.open_price - exit_price)) / pip
        realized = round(pips_diff * pip_value_per_lot_usd(pos.symbol, exit_price) * pos.volume, 2)
        self._balance = round(self._balance + realized, 2)
        self._closed_deals.append(
            {
                "ticket": ticket,
                "symbol": pos.symbol,
                "side": pos.side,
                "volume": pos.volume,
                "open_price": pos.open_price,
                "close_price": exit_price,
                "profit": realized,
                "reason": reason,
                "closed_at": utc_now_iso(),
            }
        )
        return realized

    def get_account_snapshot(self) -> AccountSnapshot:
        with self._lock:
            self._mark_positions_locked()
            floating = sum(p.profit for p in self._positions.values())
            equity = round(self._balance + floating, 2)
            margin = round(sum(p.volume * 200.0 for p in self._positions.values()), 2)
            return AccountSnapshot(
                connected=True,
                broker_mode=self.mode_name,
                trade_mode="paper",
                account_label="X4-PAPER-SIM",
                server="X4-Cloud-Paper-Engine",
                balance=round(self._balance, 2),
                equity=equity,
                margin=margin,
                margin_free=round(max(0.0, equity - margin), 2),
                currency="USD",
                heartbeat_age_sec=0.0,
            )

    def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        with self._lock:
            q = self._quotes.get(symbol.upper())
            if q is None:
                return None
            return MarketQuote(
                symbol=q.symbol,
                bid=q.bid,
                ask=q.ask,
                spread_pips=q.spread_pips,
                ts_epoch=q.ts_epoch,
                age_sec=round(max(0.0, time.time() - q.ts_epoch), 2),
            )

    def get_all_quotes(self) -> dict[str, MarketQuote]:
        now = time.time()
        with self._lock:
            return {
                sym: MarketQuote(
                    symbol=q.symbol,
                    bid=q.bid,
                    ask=q.ask,
                    spread_pips=q.spread_pips,
                    ts_epoch=q.ts_epoch,
                    age_sec=round(max(0.0, now - q.ts_epoch), 2),
                )
                for sym, q in self._quotes.items()
            }

    def get_open_positions(self) -> list[OpenPosition]:
        with self._lock:
            self._mark_positions_locked()
            return [OpenPosition(**p.to_dict()) for p in self._positions.values()]

    def place_market_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        volume: float,
        sl: Optional[float],
        tp: Optional[float],
        magic: int = 240400,
        comment: str = "",
    ) -> OrderReceipt:
        sym = symbol.upper()
        dir_side = side.upper()
        with self._lock:
            q = self._quotes.get(sym)
            if q is None:
                return OrderReceipt("REJECTED", client_order_id, message=f"Unknown paper symbol: {sym}")
            fill = q.ask if dir_side == "BUY" else q.bid
            if not math.isfinite(fill) or fill <= 0:
                return OrderReceipt("REJECTED", client_order_id, message="Invalid quote price")
            tkt = self._next_ticket
            self._next_ticket += 1
            self._positions[tkt] = OpenPosition(
                ticket=tkt,
                symbol=sym,
                side=dir_side,
                volume=round(float(volume), 2),
                open_price=fill,
                current_price=fill,
                sl=float(sl or 0.0),
                tp=float(tp or 0.0),
                profit=0.0,
                magic=magic,
                comment=comment or client_order_id,
                opened_at=utc_now_iso(),
            )
            return OrderReceipt(
                status="ACKED",
                client_order_id=client_order_id,
                broker_ticket=tkt,
                fill_price=fill,
                message="PAPER_FILLED",
            )

    def close_position(self, *, ticket: int, comment: str = "") -> OrderReceipt:
        with self._lock:
            pos = self._positions.get(int(ticket))
            if pos is None:
                return OrderReceipt("REJECTED", f"close-{ticket}", message="Position not found")
            q = self._quotes.get(pos.symbol)
            exit_px = (q.bid if pos.side == "BUY" else q.ask) if q else pos.current_price
            pnl = self._close_locked(int(ticket), exit_px, comment or "MANUAL_CLOSE")
            return OrderReceipt(
                status="ACKED",
                client_order_id=f"close-{ticket}",
                broker_ticket=int(ticket),
                fill_price=exit_px,
                message=f"PAPER_CLOSED pnl={pnl}",
                extra={"pnl": pnl},
            )
