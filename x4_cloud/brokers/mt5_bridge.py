"""HMAC-SHA256 authenticated HTTPS MT5 Cloud Bridge adapter.

Allows X4 Cloud Terminal to run 24/7 on ANY free Linux cloud host (Render,
Northflank, Docker) while MetaTrader 5 executes remotely via either:
  1. `mql5/X4_CloudBridge_EA.mq5` attached to any MT5 chart (zero Python on MT5 side), OR
  2. `bridge_agent/mt5_relay.py` running inside Wine+MT5 Docker or a local Windows/Linux box.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from x4_cloud.brokers import AccountSnapshot, MarketQuote, OpenPosition, OrderReceipt
from x4_cloud.ledger import ExecutionLedger
from x4_cloud.policy import pip_size_for_symbol

_MAX_REPLAY_SKEW_SEC = 180


def sign_bridge_payload(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Compute canonical HMAC-SHA256 signature over `<timestamp>.<raw_body>`."""
    msg = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_bridge_signature(
    *,
    secret: str,
    timestamp: str,
    signature: str,
    raw_body: bytes,
    now_epoch: Optional[float] = None,
    max_skew_sec: int = _MAX_REPLAY_SKEW_SEC,
) -> tuple[bool, str]:
    """Verify HMAC-SHA256 bridge signature with constant-time compare and replay window check."""
    if not secret:
        return False, "BRIDGE_SECRET_NOT_CONFIGURED"
    if not timestamp or not signature:
        return False, "MISSING_AUTH_HEADERS"
    try:
        ts_val = float(timestamp)
    except ValueError:
        return False, "INVALID_TIMESTAMP"
    now = time.time() if now_epoch is None else float(now_epoch)
    if abs(now - ts_val) > max_skew_sec:
        return False, "TIMESTAMP_OUT_OF_WINDOW"
    expected = sign_bridge_payload(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected.lower(), signature.strip().lower()):
        return False, "INVALID_SIGNATURE"
    return True, "OK"


class MT5BridgeBrokerAdapter:
    """Stateful cloud receiver for MT5 EA / Wine Relay heartbeats & queued order execution."""

    mode_name = "mt5_bridge"

    def __init__(
        self,
        ledger: ExecutionLedger,
        *,
        stale_timeout_sec: float = 45.0,
        command_ttl_sec: int = 60,
    ) -> None:
        self._ledger = ledger
        self._stale_timeout_sec = float(stale_timeout_sec)
        self._command_ttl_sec = int(command_ttl_sec)
        self._lock = threading.RLock()
        self._last_sync_epoch: Optional[float] = None
        self._account: dict[str, Any] = {}
        self._quotes: dict[str, MarketQuote] = {}
        self._price_history: dict[str, deque[float]] = {}
        self._positions: list[OpenPosition] = []
        self._bridge_agent_version: str = ""

    def ingest_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process a signed heartbeat/state push from MT5 EA or Python relay and return pending commands."""
        now = time.time()
        acct = payload.get("account") or {}
        raw_quotes = payload.get("quotes") or {}
        raw_positions = payload.get("positions") or []

        with self._lock:
            self._last_sync_epoch = now
            self._bridge_agent_version = str(payload.get("agent_version", "unknown"))
            self._account = {
                "trade_mode": str(acct.get("trade_mode", "unknown")).strip().lower(),
                "account_label": str(acct.get("login", "unlinked")),
                "server": str(acct.get("server", "unknown")),
                "balance": float(acct.get("balance", 0.0) or 0.0),
                "equity": float(acct.get("equity", 0.0) or 0.0),
                "margin": float(acct.get("margin", 0.0) or 0.0),
                "margin_free": float(acct.get("margin_free", 0.0) or 0.0),
                "currency": str(acct.get("currency", "USD") or "USD"),
            }

            for sym_raw, q in raw_quotes.items():
                sym = str(sym_raw).strip().upper()
                bid = float(q.get("bid", 0.0) or 0.0)
                ask = float(q.get("ask", 0.0) or 0.0)
                if bid <= 0 or ask <= 0 or ask < bid:
                    continue
                pip = pip_size_for_symbol(sym)
                spread_pips = round((ask - bid) / pip, 2)
                self._quotes[sym] = MarketQuote(
                    symbol=sym,
                    bid=bid,
                    ask=ask,
                    spread_pips=spread_pips,
                    ts_epoch=now,
                    age_sec=0.0,
                )
                hist = self._price_history.setdefault(sym, deque(maxlen=250))
                hist.append((bid + ask) / 2.0)

            parsed_pos: list[OpenPosition] = []
            for p in raw_positions:
                try:
                    parsed_pos.append(
                        OpenPosition(
                            ticket=int(p["ticket"]),
                            symbol=str(p["symbol"]).strip().upper(),
                            side=str(p["side"]).strip().upper(),
                            volume=float(p["volume"]),
                            open_price=float(p["open_price"]),
                            current_price=float(p.get("current_price", p["open_price"])),
                            sl=float(p.get("sl", 0.0) or 0.0),
                            tp=float(p.get("tp", 0.0) or 0.0),
                            profit=float(p.get("profit", 0.0) or 0.0),
                            magic=int(p.get("magic", 0) or 0),
                            comment=str(p.get("comment", "") or ""),
                            opened_at=str(p.get("opened_at", "") or ""),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            self._positions = parsed_pos

        pending = self._ledger.fetch_pending_bridge_commands()
        return {
            "ok": True,
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
            "commands": pending,
        }

    def ingest_ack(self, payload: dict[str, Any]) -> bool:
        cmd_id = str(payload.get("command_id", "")).strip()
        status = str(payload.get("status", "UNKNOWN")).strip().upper()
        if not cmd_id:
            return False
        updated = self._ledger.ack_bridge_command(cmd_id, status, payload)
        self._ledger.record_audit(
            actor="mt5_bridge_agent",
            action="BRIDGE_COMMAND_ACK",
            details={"command_id": cmd_id, "status": status, "ticket": payload.get("broker_ticket")},
        )
        return updated

    def get_price_series(self, symbol: str) -> list[float]:
        with self._lock:
            return list(self._price_history.get(symbol.upper(), ()))

    def tick(self) -> None:
        # Quotes arrive via signed pushes from MT5 EA / Wine relay.
        return None

    def get_bridge_diagnostics(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._last_sync_epoch is None else round(max(0.0, time.time() - self._last_sync_epoch), 2)
            connected = age is not None and age <= self._stale_timeout_sec
            return {
                "connected": connected,
                "heartbeat_age_sec": age,
                "stale_timeout_sec": self._stale_timeout_sec,
                "agent_version": self._bridge_agent_version,
                "quotes_tracked": sorted(self._quotes.keys()),
                "pending_commands": len(self._ledger.fetch_pending_bridge_commands()),
            }

    def get_account_snapshot(self) -> AccountSnapshot:
        with self._lock:
            if self._last_sync_epoch is None:
                return AccountSnapshot(
                    connected=False,
                    broker_mode=self.mode_name,
                    trade_mode="unknown",
                    account_label="WAITING_FOR_MT5_BRIDGE",
                    server="disconnected",
                    balance=0.0,
                    equity=0.0,
                    heartbeat_age_sec=None,
                )
            age = round(max(0.0, time.time() - self._last_sync_epoch), 2)
            connected = age <= self._stale_timeout_sec
            return AccountSnapshot(
                connected=connected,
                broker_mode=self.mode_name,
                trade_mode=self._account.get("trade_mode", "unknown") if connected else "unknown",
                account_label=str(self._account.get("account_label", "unlinked")),
                server=str(self._account.get("server", "unknown")),
                balance=float(self._account.get("balance", 0.0)),
                equity=float(self._account.get("equity", 0.0)),
                margin=float(self._account.get("margin", 0.0)),
                margin_free=float(self._account.get("margin_free", 0.0)),
                currency=str(self._account.get("currency", "USD")),
                heartbeat_age_sec=age,
            )

    def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        now = time.time()
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
                age_sec=round(max(0.0, now - q.ts_epoch), 2),
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
            return [OpenPosition(**p.to_dict()) for p in self._positions]

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
        snap = self.get_account_snapshot()
        if not snap.connected:
            return OrderReceipt("REJECTED", client_order_id, message="MT5 bridge heartbeat stale or disconnected")
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self._command_ttl_sec)).isoformat()
        self._ledger.enqueue_bridge_command(
            command_id=client_order_id,
            action="OPEN_MARKET",
            payload={
                "symbol": symbol.upper(),
                "side": side.upper(),
                "volume": round(float(volume), 2),
                "sl": float(sl or 0.0),
                "tp": float(tp or 0.0),
                "magic": int(magic),
                "comment": comment or client_order_id[:24],
            },
            expires_at=expires_at,
        )
        return OrderReceipt(
            status="QUEUED",
            client_order_id=client_order_id,
            message="Queued for MT5 Cloud Bridge EA execution",
        )

    def close_position(self, *, ticket: int, comment: str = "") -> OrderReceipt:
        snap = self.get_account_snapshot()
        if not snap.connected:
            return OrderReceipt("REJECTED", f"close-{ticket}", message="MT5 bridge disconnected")
        cmd_id = f"close-{ticket}-{int(time.time())}"
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self._command_ttl_sec)).isoformat()
        self._ledger.enqueue_bridge_command(
            command_id=cmd_id,
            action="CLOSE_POSITION",
            payload={"ticket": int(ticket), "comment": comment or "X4_CLOUD_CLOSE"},
            expires_at=expires_at,
        )
        return OrderReceipt(
            status="QUEUED",
            client_order_id=cmd_id,
            broker_ticket=int(ticket),
            message="Close command queued for MT5 Bridge",
        )
