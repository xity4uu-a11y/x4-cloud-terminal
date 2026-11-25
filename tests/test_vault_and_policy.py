"""Tests for cross-platform AES-256-GCM vault and fail-closed risk policy engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from x4_cloud.policy import RiskGate, load_policy
from x4_cloud.vault import CrossPlatformVault, redact_secrets

POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "demo_default.yaml"


def test_vault_roundtrip_wrong_key_and_tamper_detection(tmp_path: Path) -> None:
    vpath = tmp_path / "secrets.vault.json"
    vault = CrossPlatformVault(vpath, "super-strong-passphrase-2026")
    vault.set_secret("BRIDGE_SECRET", "s3cr3t-value-xyz")
    assert vault.get_secret("BRIDGE_SECRET") == "s3cr3t-value-xyz"
    assert vault.list_keys() == ["BRIDGE_SECRET"]

    # Wrong passphrase must fail closed
    bad_vault = CrossPlatformVault(vpath, "wrong-passphrase-2026")
    with pytest.raises(PermissionError):
        bad_vault.get_secret("BRIDGE_SECRET")

    # Moving ciphertext to another key name must fail AAD verification
    raw = json.loads(vpath.read_text(encoding="utf-8"))
    raw["secrets"]["TRANSPLANTED_KEY"] = raw["secrets"]["BRIDGE_SECRET"]
    vpath.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PermissionError):
        vault.get_secret("TRANSPLANTED_KEY")

    assert vault.delete_secret("BRIDGE_SECRET") is True
    assert vault.get_secret("BRIDGE_SECRET") is None


def test_secret_redaction() -> None:
    msg = "Authorization: Bearer abc123.def456 token=mysecret99 password: topsecret"
    cleaned = redact_secrets(msg)
    assert "abc123.def456" not in cleaned
    assert "mysecret99" not in cleaned
    assert "topsecret" not in cleaned


def test_risk_gate_fail_closed_invariants() -> None:
    policy = load_policy(POLICY_PATH)
    assert len(policy.policy_hash) == 64
    gate = RiskGate(policy, real_execution_unlock=False)

    # 1. REAL mode double-locked
    ev_real = gate.evaluate_order(
        trade_mode="real",
        symbol="XAUUSD",
        side="BUY",
        entry_price=2685.0,
        stop_loss=2680.0,
        requested_volume=0.05,
        equity=10000.0,
        day_start_equity=10000.0,
        peak_equity=10000.0,
        open_positions_count=0,
        spread_pips=1.5,
        quote_age_sec=1.0,
        kill_switch_active=False,
    )
    assert not ev_real.approved
    assert ev_real.reason == "R_REAL_MODE_DOUBLE_LOCKED"

    # 2. Missing SL rejected
    ev_nosl = gate.evaluate_order(
        trade_mode="demo",
        symbol="XAUUSD",
        side="BUY",
        entry_price=2685.0,
        stop_loss=None,
        requested_volume=0.05,
        equity=10000.0,
        day_start_equity=10000.0,
        peak_equity=10000.0,
        open_positions_count=0,
        spread_pips=1.5,
        quote_age_sec=1.0,
        kill_switch_active=False,
    )
    assert not ev_nosl.approved
    assert ev_nosl.reason == "R_STOP_LOSS_REQUIRED"

    # 3. Daily loss breach rejected
    ev_dd = gate.evaluate_order(
        trade_mode="demo",
        symbol="XAUUSD",
        side="BUY",
        entry_price=2685.0,
        stop_loss=2682.0,
        requested_volume=0.05,
        equity=9750.0,
        day_start_equity=10000.0,
        peak_equity=10000.0,
        open_positions_count=0,
        spread_pips=1.5,
        quote_age_sec=1.0,
        kill_switch_active=False,
    )
    assert not ev_dd.approved
    assert ev_dd.reason.startswith("R_DAILY_LOSS_LIMIT_BREACHED")

    # 4. Stale quote rejected
    ev_stale = gate.evaluate_order(
        trade_mode="demo",
        symbol="XAUUSD",
        side="BUY",
        entry_price=2685.0,
        stop_loss=2682.0,
        requested_volume=0.05,
        equity=10000.0,
        day_start_equity=10000.0,
        peak_equity=10000.0,
        open_positions_count=0,
        spread_pips=1.5,
        quote_age_sec=120.0,
        kill_switch_active=False,
    )
    assert not ev_stale.approved
    assert ev_stale.reason.startswith("R_STALE_QUOTE")

    # 5. Valid demo order caps volume to 0.5% risk ($50 on $10k; 20 pips * $10/lot = $200/lot -> max 0.25 lot)
    ev_ok = gate.evaluate_order(
        trade_mode="demo",
        symbol="XAUUSD",
        side="BUY",
        entry_price=2685.0,
        stop_loss=2683.0,  # 20 pips ($0.10/pip)
        requested_volume=0.50,
        equity=10000.0,
        day_start_equity=10000.0,
        peak_equity=10000.0,
        open_positions_count=0,
        spread_pips=1.5,
        quote_age_sec=1.0,
        kill_switch_active=False,
    )
    assert ev_ok.approved
    assert ev_ok.approved_volume == pytest.approx(0.25)
    assert ev_ok.estimated_risk_usd == pytest.approx(50.0)
