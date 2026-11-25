"""End-to-end Starlette HTTP API tests for X4 Cloud Terminal (Paper & MT5 Bridge modes)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from starlette.testclient import TestClient

from x4_cloud.brokers.mt5_bridge import sign_bridge_payload
from x4_cloud.config import CloudSettings
from x4_cloud.server import create_app

POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "demo_default.yaml"


def _make_settings(tmp_path: Path, *, broker_mode: str = "paper") -> CloudSettings:
    return CloudSettings(
        data_dir=tmp_path / "data",
        policy_path=POLICY_PATH,
        broker_mode=broker_mode,
        api_token="unit-test-operator-token",
        bridge_secret="unit-test-bridge-hmac-secret",
        symbols=("XAUUSD", "EURUSD"),
        poll_interval_sec=1.0,
        auto_start_engine=False,
        real_execution_unlock=False,
    )


def test_paper_mode_api_end_to_end(tmp_path: Path) -> None:
    app = create_app(settings=_make_settings(tmp_path, broker_mode="paper"))
    headers = {"Authorization": "Bearer unit-test-operator-token"}

    with TestClient(app) as client:
        # Public liveness probe works without auth
        hz = client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json()["ok"] is True

        # Protected status rejects missing/bad auth
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401

        st = client.get("/api/status", headers=headers)
        assert st.status_code == 200
        data = st.json()
        assert data["account"]["broker_mode"] == "paper"
        assert data["audit_chain_valid"] is True

        # Place valid risk-checked order
        q = data["quotes"]["XAUUSD"]
        sl = round(q["ask"] - 3.0, 2)
        tp = round(q["ask"] + 5.0, 2)
        ord_res = client.post(
            "/api/orders",
            headers=headers,
            json={"symbol": "XAUUSD", "side": "BUY", "sl": sl, "tp": tp},
        )
        assert ord_res.status_code == 200
        ticket = ord_res.json()["receipt"]["broker_ticket"]
        assert ticket is not None

        # Missing SL rejected by RiskGate (422)
        bad_ord = client.post(
            "/api/orders",
            headers=headers,
            json={"symbol": "XAUUSD", "side": "BUY", "sl": None},
        )
        assert bad_ord.status_code == 422

        # Close position
        cl = client.post(f"/api/positions/{ticket}/close", headers=headers)
        assert cl.status_code == 200
        assert cl.json()["ok"] is True

        # Kill switch blocks engine start until reset
        ks = client.post("/api/engine/kill-switch", headers=headers, json={"active": True, "reason": "drill"})
        assert ks.status_code == 200
        assert client.post("/api/engine/start", headers=headers, json={}).status_code == 409

        client.post("/api/engine/kill-switch", headers=headers, json={"active": False})
        assert client.post("/api/engine/start", headers=headers, json={"strategy": "ema_rsi_pullback"}).status_code == 200
        assert client.post("/api/engine/stop", headers=headers, json={}).status_code == 200


def test_mt5_bridge_mode_signed_sync_order_and_ack(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path, broker_mode="mt5_bridge")
    app = create_app(settings=cfg)
    op_headers = {"Authorization": f"Bearer {cfg.api_token}"}

    def signed_headers(raw_body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        sig = sign_bridge_payload(cfg.bridge_secret, ts, raw_body)
        return {
            "Content-Type": "application/json",
            "X-X4-Timestamp": ts,
            "X-X4-Signature": sig,
        }

    with TestClient(app) as client:
        sync_payload = {
            "agent_version": "X4_CloudBridge_EA/1.0",
            "account": {
                "login": 5550001,
                "server": "DemoServer-01",
                "trade_mode": "demo",
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "margin_free": 10000.0,
                "currency": "USD",
            },
            "quotes": {"XAUUSD": {"bid": 2685.00, "ask": 2685.15}},
            "positions": [],
        }
        raw_sync = json.dumps(sync_payload).encode("utf-8")

        # Unsigned sync rejected
        assert client.post("/api/bridge/sync", content=raw_sync).status_code == 401

        # Signed sync accepted
        r1 = client.post("/api/bridge/sync", content=raw_sync, headers=signed_headers(raw_sync))
        assert r1.status_code == 200
        assert r1.json()["commands"] == []

        # Operator queues a demo order
        ord_res = client.post(
            "/api/orders",
            headers=op_headers,
            json={"symbol": "XAUUSD", "side": "BUY", "sl": 2682.00, "tp": 2692.00},
        )
        assert ord_res.status_code == 200
        assert ord_res.json()["receipt"]["status"] == "QUEUED"
        cmd_id = ord_res.json()["receipt"]["client_order_id"]

        # Next MT5 EA heartbeat receives queued order
        r2 = client.post("/api/bridge/sync", content=raw_sync, headers=signed_headers(raw_sync))
        assert r2.status_code == 200
        cmds = r2.json()["commands"]
        assert len(cmds) == 1
        assert cmds[0]["command_id"] == cmd_id

        # MT5 EA posts signed ACK
        ack_raw = json.dumps({"command_id": cmd_id, "status": "ACKED", "broker_ticket": 884422}).encode("utf-8")
        r3 = client.post("/api/bridge/ack", content=ack_raw, headers=signed_headers(ack_raw))
        assert r3.status_code == 200
        assert r3.json()["ok"] is True

        # Command queue is now empty
        r4 = client.post("/api/bridge/sync", content=raw_sync, headers=signed_headers(raw_sync))
        assert r4.json()["commands"] == []
