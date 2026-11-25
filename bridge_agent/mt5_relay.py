"""Lightweight MT5-to-X4-Cloud HTTPS Relay Agent (runs on Windows OR Linux+Wine Python).

Bridges a local MetaTrader 5 terminal (`terminal64.exe`) to a remote X4 Cloud Terminal
instance over HMAC-SHA256 authenticated HTTPS — zero inbound ports, zero Windows Server VM.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | mt5-relay | %(message)s")
logger = logging.getLogger("x4_mt5_relay")


def sign_body(secret: str, ts: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), ts.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()


def signed_post(client: httpx.Client, base_url: str, secret: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    sig = sign_body(secret, ts, raw)
    resp = client.post(
        f"{base_url.rstrip('/')}{path}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-X4-Timestamp": ts,
            "X-X4-Signature": sig,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def collect_mt5_state(mt5: Any, symbols: list[str]) -> dict[str, Any]:
    acct = mt5.account_info()
    if acct is None:
        raise RuntimeError("MT5 account_info() returned None (is terminal logged in?)")
    mode_map = {0: "demo", 1: "demo", 2: "real"}
    trade_mode = mode_map.get(int(getattr(acct, "trade_mode", -1)), "unknown")

    quotes: dict[str, dict[str, float]] = {}
    for sym in symbols:
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick and tick.bid > 0 and tick.ask > 0:
            quotes[sym] = {"bid": float(tick.bid), "ask": float(tick.ask)}

    positions: list[dict[str, Any]] = []
    for p in (mt5.positions_get() or ()):
        positions.append(
            {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "side": "BUY" if int(p.type) == 0 else "SELL",
                "volume": float(p.volume),
                "open_price": float(p.price_open),
                "current_price": float(p.price_current),
                "sl": float(p.sl or 0.0),
                "tp": float(p.tp or 0.0),
                "profit": float(p.profit or 0.0),
                "magic": int(p.magic or 0),
                "comment": str(p.comment or ""),
            }
        )

    return {
        "agent_version": "x4-python-relay/1.0",
        "account": {
            "login": int(acct.login),
            "server": str(acct.server),
            "trade_mode": trade_mode,
            "balance": float(acct.balance),
            "equity": float(acct.equity),
            "margin": float(acct.margin),
            "margin_free": float(acct.margin_free),
            "currency": str(acct.currency or "USD"),
        },
        "quotes": quotes,
        "positions": positions,
    }


def execute_command(mt5: Any, cmd: dict[str, Any], *, allow_real: bool) -> dict[str, Any]:
    cmd_id = str(cmd["command_id"])
    action = str(cmd["action"])
    p = cmd.get("payload") or {}

    acct = mt5.account_info()
    if acct is None:
        return {"command_id": cmd_id, "status": "REJECTED", "message": "MT5 disconnected"}
    if int(getattr(acct, "trade_mode", 2)) == 2 and not allow_real:
        return {"command_id": cmd_id, "status": "REJECTED", "message": "REAL_LOCKED_IN_RELAY"}

    if action == "OPEN_MARKET":
        sym = str(p["symbol"])
        side = str(p["side"]).upper()
        vol = float(p["volume"])
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return {"command_id": cmd_id, "status": "REJECTED", "message": f"No tick for {sym}"}
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if side == "BUY" else tick.bid)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": vol,
            "type": order_type,
            "price": price,
            "sl": float(p.get("sl") or 0.0),
            "tp": float(p.get("tp") or 0.0),
            "deviation": 30,
            "magic": int(p.get("magic") or 240400),
            "comment": str(p.get("comment") or "x4_cloud")[:24],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        ok = res is not None and int(getattr(res, "retcode", 0)) in (10009, 10010)
        return {
            "command_id": cmd_id,
            "status": "ACKED" if ok else "REJECTED",
            "broker_ticket": int(getattr(res, "order", 0) or 0) if res else None,
            "retcode": int(getattr(res, "retcode", 0) or 0) if res else None,
            "message": str(getattr(res, "comment", "no_response")) if res else "order_send None",
        }

    if action == "CLOSE_POSITION":
        ticket = int(p["ticket"])
        matches = mt5.positions_get(ticket=ticket) or ()
        if not matches:
            return {"command_id": cmd_id, "status": "REJECTED", "message": "Position not found"}
        pos = matches[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        close_px = float(tick.bid if pos.type == 0 else tick.ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": close_type,
            "price": close_px,
            "deviation": 30,
            "magic": int(pos.magic),
            "comment": "x4_close",
        }
        res = mt5.order_send(req)
        ok = res is not None and int(getattr(res, "retcode", 0)) in (10009, 10010)
        return {
            "command_id": cmd_id,
            "status": "ACKED" if ok else "REJECTED",
            "broker_ticket": ticket,
            "retcode": int(getattr(res, "retcode", 0) or 0) if res else None,
            "message": str(getattr(res, "comment", "no_response")) if res else "order_send None",
        }

    return {"command_id": cmd_id, "status": "REJECTED", "message": f"Unknown action: {action}"}


def main() -> None:
    import MetaTrader5 as mt5

    base_url = os.environ["X4_CLOUD_URL"].strip()
    secret = os.environ["X4_BRIDGE_SECRET"].strip()
    symbols = [s.strip() for s in os.getenv("X4_SYMBOLS", "XAUUSD,EURUSD,GBPUSD,USDJPY").split(",") if s.strip()]
    interval = max(2.0, float(os.getenv("X4_SYNC_INTERVAL_SEC", "5.0")))
    allow_real = os.getenv("X4_RELAY_ALLOW_REAL", "false").strip().lower() == "true"

    logger.info("Connecting to local MetaTrader 5 IPC...")
    if not mt5.initialize():
        raise SystemExit(f"MetaTrader5 initialize() failed: {mt5.last_error()}")

    logger.info("MT5 Relay active -> %s (symbols=%s, allow_real=%s)", base_url, symbols, allow_real)
    with httpx.Client() as client:
        while True:
            try:
                state = collect_mt5_state(mt5, symbols)
                res = signed_post(client, base_url, secret, "/api/bridge/sync", state)
                for cmd in res.get("commands") or []:
                    ack = execute_command(mt5, cmd, allow_real=allow_real)
                    signed_post(client, base_url, secret, "/api/bridge/ack", ack)
                    logger.info("Executed command %s -> %s", cmd.get("command_id"), ack.get("status"))
            except Exception as exc:
                logger.warning("Relay sync cycle warning: %s", exc)
            time.sleep(interval)


if __name__ == "__main__":
    main()
