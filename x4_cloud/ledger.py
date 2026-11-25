"""SQLite WAL ledger for trades, kill-switch state, bridge command queue, and hash-chained audit logs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionLedger:
    """Thread-safe SQLite WAL persistence store for X4 Cloud Terminal."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=FULL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT UNIQUE NOT NULL,
                broker_mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                volume REAL NOT NULL,
                entry_price REAL NOT NULL,
                sl REAL,
                tp REAL,
                status TEXT NOT NULL,
                broker_ticket INTEGER,
                fill_price REAL,
                pnl REAL DEFAULT 0.0,
                strategy_id TEXT,
                policy_hash TEXT,
                message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bridge_commands (
                command_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ack_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Runtime State KV (kill switch, peak equity, day start equity) ────────

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), now),
            )
            self._conn.commit()

    def is_kill_switch_active(self) -> bool:
        return (self.get_state("kill_switch_active", "false") or "false").lower() == "true"

    def set_kill_switch(self, active: bool, *, actor: str, reason: str) -> None:
        self.set_state("kill_switch_active", "true" if active else "false")
        self.set_state("kill_switch_reason", reason if active else "")
        self.record_audit(
            actor=actor,
            action="KILL_SWITCH_ENABLED" if active else "KILL_SWITCH_RESET",
            details={"active": active, "reason": reason},
        )

    # ── Orders ───────────────────────────────────────────────────────────────

    def record_order(
        self,
        *,
        client_order_id: str,
        broker_mode: str,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        sl: Optional[float],
        tp: Optional[float],
        status: str,
        broker_ticket: Optional[int] = None,
        fill_price: Optional[float] = None,
        pnl: float = 0.0,
        strategy_id: str = "manual",
        policy_hash: str = "",
        message: str = "",
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    client_order_id, broker_mode, symbol, side, volume, entry_price,
                    sl, tp, status, broker_ticket, fill_price, pnl,
                    strategy_id, policy_hash, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    status = excluded.status,
                    broker_ticket = COALESCE(excluded.broker_ticket, orders.broker_ticket),
                    fill_price = COALESCE(excluded.fill_price, orders.fill_price),
                    pnl = excluded.pnl,
                    message = excluded.message,
                    updated_at = excluded.updated_at
                """,
                (
                    client_order_id,
                    broker_mode,
                    symbol,
                    side,
                    float(volume),
                    float(entry_price),
                    float(sl) if sl is not None else None,
                    float(tp) if tp is not None else None,
                    status,
                    broker_ticket,
                    fill_price,
                    float(pnl),
                    strategy_id,
                    policy_hash,
                    message,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Bridge Command Queue ─────────────────────────────────────────────────

    def enqueue_bridge_command(
        self,
        *,
        command_id: str,
        action: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        encoded = json.dumps(payload, sort_keys=True)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO bridge_commands (
                    command_id, action, payload_json, status, created_at, expires_at, ack_json, updated_at
                ) VALUES (?, ?, ?, 'PENDING', ?, ?, NULL, ?)
                """,
                (command_id, action, encoded, now, expires_at, now),
            )
            self._conn.commit()
        return {
            "command_id": command_id,
            "action": action,
            "payload": payload,
            "status": "PENDING",
            "created_at": now,
            "expires_at": expires_at,
        }

    def fetch_pending_bridge_commands(self) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE bridge_commands SET status = 'EXPIRED', updated_at = ? WHERE status = 'PENDING' AND expires_at < ?",
                (now, now),
            )
            rows = self._conn.execute(
                "SELECT * FROM bridge_commands WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 25"
            ).fetchall()
            self._conn.commit()
            result: list[dict[str, Any]] = []
            for r in rows:
                result.append(
                    {
                        "command_id": r["command_id"],
                        "action": r["action"],
                        "payload": json.loads(r["payload_json"]),
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                    }
                )
            return result

    def ack_bridge_command(self, command_id: str, status: str, ack_payload: dict[str, Any]) -> bool:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE bridge_commands
                SET status = ?, ack_json = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (status.upper(), json.dumps(ack_payload, sort_keys=True), now, command_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_bridge_commands(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bridge_commands ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "command_id": r["command_id"],
                        "action": r["action"],
                        "payload": json.loads(r["payload_json"]),
                        "status": r["status"],
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                        "ack": json.loads(r["ack_json"]) if r["ack_json"] else None,
                    }
                )
            return out

    # ── SHA-256 Hash-Chained Security Audit Trail ────────────────────────────

    def record_audit(self, *, actor: str, action: str, details: dict[str, Any]) -> str:
        now = utc_now_iso()
        details_json = json.dumps(details, sort_keys=True, default=str)
        with self._lock:
            last = self._conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(last["entry_hash"]) if last else ("0" * 64)
            material = f"{now}|{actor}|{action}|{details_json}|{prev_hash}".encode("utf-8")
            entry_hash = hashlib.sha256(material).hexdigest()
            self._conn.execute(
                """
                INSERT INTO audit_log (ts_utc, actor, action, details_json, prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, actor, action, details_json, prev_hash, entry_hash),
            )
            self._conn.commit()
            return entry_hash

    def verify_audit_chain(self) -> bool:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        expected_prev = "0" * 64
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return False
            material = (
                f"{row['ts_utc']}|{row['actor']}|{row['action']}|{row['details_json']}|{row['prev_hash']}"
            ).encode("utf-8")
            calc = hashlib.sha256(material).hexdigest()
            if calc != row["entry_hash"]:
                return False
            expected_prev = calc
        return True

    def list_audit(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts_utc, actor, action, details_json, entry_hash FROM audit_log ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "ts_utc": r["ts_utc"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "details": json.loads(r["details_json"]),
                    "entry_hash": r["entry_hash"],
                }
                for r in rows
            ]
