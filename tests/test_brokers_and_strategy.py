"""Tests for PaperBrokerAdapter, MT5BridgeBrokerAdapter HMAC auth, ledger audit chain, and strategies."""

from __future__ import annotations

from pathlib import Path

from x4_cloud.brokers.mt5_bridge import (
    MT5BridgeBrokerAdapter,
    sign_bridge_payload,
    verify_bridge_signature,
)
from x4_cloud.brokers.paper import PaperBrokerAdapter
from x4_cloud.ledger import ExecutionLedger
from x4_cloud.strategy import evaluate_ema_rsi_pullback, evaluate_keltner_atr_breakout


def test_paper_broker_lifecycle_and_tp_trigger() -> None:
    broker = PaperBrokerAdapter(initial_balance=10000.0, symbols=("XAUUSD",), seed=42)
    broker.inject_quote("XAUUSD", bid=2680.00, ask=2680.20)

    rc = broker.place_market_order(
        client_order_id="ord-1",
        symbol="XAUUSD",
        side="BUY",
        volume=0.10,
        sl=2675.00,
        tp=2685.20,
    )
    assert rc.status == "ACKED"
    assert len(broker.get_open_positions()) == 1

    # Move quote above TP -> auto-closes profitably
    broker.inject_quote("XAUUSD", bid=2686.00, ask=2686.20)
    assert len(broker.get_open_positions()) == 0
    snap = broker.get_account_snapshot()
    assert snap.balance > 10000.0


def test_bridge_hmac_signing_replay_and_command_queue(tmp_path: Path) -> None:
    secret = "bridge-test-secret-0123456789"
    body = b'{"account":{"login":12345}}'
    ts = "1700000000"
    sig = sign_bridge_payload(secret, ts, body)

    ok, reason = verify_bridge_signature(
        secret=secret, timestamp=ts, signature=sig, raw_body=body, now_epoch=1700000020
    )
    assert ok and reason == "OK"

    # Replay outside 180s window rejected
    ok_old, reason_old = verify_bridge_signature(
        secret=secret, timestamp=ts, signature=sig, raw_body=body, now_epoch=1700000500
    )
    assert not ok_old and reason_old == "TIMESTAMP_OUT_OF_WINDOW"

    # Tampered body rejected
    ok_bad, reason_bad = verify_bridge_signature(
        secret=secret, timestamp=ts, signature=sig, raw_body=b'{"account":{"login":99999}}', now_epoch=1700000010
    )
    assert not ok_bad and reason_bad == "INVALID_SIGNATURE"

    ledger = ExecutionLedger(tmp_path / "bridge.db")
    try:
        adapter = MT5BridgeBrokerAdapter(ledger=ledger)
        assert not adapter.get_account_snapshot().connected

        sync_res = adapter.ingest_sync(
            {
                "agent_version": "X4_CloudBridge_EA/1.0",
                "account": {
                    "login": 998877,
                    "server": "DemoBroker-MT5",
                    "trade_mode": "demo",
                    "balance": 5000.0,
                    "equity": 5012.5,
                },
                "quotes": {"XAUUSD": {"bid": 2680.0, "ask": 2680.18}},
                "positions": [],
            }
        )
        assert sync_res["ok"] is True
        assert adapter.get_account_snapshot().connected is True

        rc = adapter.place_market_order(
            client_order_id="cmd-demo-1",
            symbol="XAUUSD",
            side="BUY",
            volume=0.05,
            sl=2675.0,
            tp=2690.0,
        )
        assert rc.status == "QUEUED"

        # Next sync returns pending command
        sync2 = adapter.ingest_sync(
            {
                "agent_version": "X4_CloudBridge_EA/1.0",
                "account": {"login": 998877, "server": "DemoBroker-MT5", "trade_mode": "demo", "balance": 5000.0, "equity": 5012.5},
                "quotes": {"XAUUSD": {"bid": 2680.1, "ask": 2680.28}},
                "positions": [],
            }
        )
        assert len(sync2["commands"]) == 1
        assert sync2["commands"][0]["command_id"] == "cmd-demo-1"

        # Acknowledge removes it from pending queue
        assert adapter.ingest_ack({"command_id": "cmd-demo-1", "status": "ACKED", "broker_ticket": 777001}) is True
        assert ledger.fetch_pending_bridge_commands() == []
        assert ledger.verify_audit_chain() is True
    finally:
        ledger.close()


def test_deterministic_strategies_trigger_on_clean_setups() -> None:
    # Upward trend -> brief consolidation inside Keltner channel -> clean breakout bar
    uptrend = [2600.0 + i * 0.4 for i in range(45)]
    consolidation = [uptrend[-1] - 0.1 + (0.2 if i % 2 == 0 else -0.2) for i in range(15)]
    series = uptrend + consolidation + [uptrend[-1] + 4.0]
    sig_k = evaluate_keltner_atr_breakout("XAUUSD", series)
    assert sig_k is not None
    assert sig_k.side == "BUY"
    assert sig_k.stop_loss < sig_k.entry_price < sig_k.take_profit

    # Uptrend followed by sharp short pullback to depress RSI below 34 while EMA12 > EMA34
    base = [1.0500 + i * 0.0010 for i in range(42)]
    pullback = [base[-1] - (i + 1) * 0.00055 for i in range(12)]
    sig_r = evaluate_ema_rsi_pullback("EURUSD", base + pullback)
    assert sig_r is not None
    assert sig_r.side == "BUY"
