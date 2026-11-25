"""Cross-platform trading engine supervisor (zero Windows API / taskkill dependencies)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from x4_cloud.brokers import BrokerAdapter, OrderReceipt
from x4_cloud.config import CloudSettings
from x4_cloud.ledger import ExecutionLedger, utc_now_iso
from x4_cloud.policy import RiskEvaluation, RiskGate, TradingPolicy, check_symbols_whitelist
from x4_cloud.strategy import STRATEGIES, generate_signal

logger = logging.getLogger("x4_cloud.supervisor")


class CloudTradingSupervisor:
    """Runs the deterministic evaluation loop, enforces fail-closed risk gates, and emits webhook alerts."""

    def __init__(
        self,
        *,
        settings: CloudSettings,
        policy: TradingPolicy,
        ledger: ExecutionLedger,
        broker: BrokerAdapter,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.ledger = ledger
        self.broker = broker
        self.risk_gate = RiskGate(policy, real_execution_unlock=settings.real_execution_unlock)
        self.active_strategy = (
            settings.default_strategy if settings.default_strategy in STRATEGIES else "keltner_atr_breakout"
        )
        self.symbols = check_symbols_whitelist(settings.symbols, policy)
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[str] = None
        self._last_cycle_at: Optional[str] = None
        self._cycle_count = 0
        self._last_signal_summary: str = "Engine idle (safe-halted on boot)."

        self.ledger.record_audit(
            actor="supervisor",
            action="BOOT_SAFE_HALT",
            details={
                "broker_mode": broker.mode_name,
                "policy_id": policy.policy_id,
                "policy_hash": policy.policy_hash[:16],
            },
        )

    def _update_equity_watermarks(self, equity: float) -> tuple[float, float]:
        if equity <= 0:
            return 0.0, 0.0
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stored_day = self.ledger.get_state("equity_day_utc")
        if stored_day != today_utc or not self.ledger.get_state("day_start_equity"):
            self.ledger.set_state("equity_day_utc", today_utc)
            self.ledger.set_state("day_start_equity", f"{equity:.2f}")
        day_start = float(self.ledger.get_state("day_start_equity", f"{equity:.2f}") or equity)

        peak = float(self.ledger.get_state("peak_equity", f"{equity:.2f}") or equity)
        if equity > peak:
            peak = equity
            self.ledger.set_state("peak_equity", f"{peak:.2f}")
        return day_start, peak

    def notify_webhook(self, title: str, message: str) -> None:
        url = self.settings.alert_webhook_url
        if not url:
            return
        try:
            payload = {"content": f"**[X4 Cloud]** {title}\n{message}", "text": f"[X4 Cloud] {title}: {message}"}
            httpx.post(url, json=payload, timeout=5.0)
        except Exception as exc:
            logger.warning("Webhook alert delivery failed: %s", type(exc).__name__)

    def start(self, *, actor: str = "operator", strategy: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if self.ledger.is_kill_switch_active():
                return {"ok": False, "reason": "Kill switch is active; reset kill switch before starting."}
            if strategy and strategy in STRATEGIES:
                self.active_strategy = strategy
            if self._running:
                return {"ok": True, "running": True, "strategy": self.active_strategy}
            self._running = True
            self._started_at = utc_now_iso()
            self._thread = threading.Thread(target=self._loop, name="x4-cloud-engine", daemon=True)
            self._thread.start()
            self.ledger.record_audit(
                actor=actor,
                action="ENGINE_STARTED",
                details={"strategy": self.active_strategy, "broker_mode": self.broker.mode_name},
            )
            return {"ok": True, "running": True, "strategy": self.active_strategy}

    def stop(self, *, actor: str = "operator", reason: str = "manual_stop") -> dict[str, Any]:
        with self._lock:
            self._running = False
            self.ledger.record_audit(
                actor=actor,
                action="ENGINE_STOPPED",
                details={"reason": reason},
            )
            return {"ok": True, "running": False}

    def trigger_kill_switch(self, *, actor: str, reason: str) -> dict[str, Any]:
        with self._lock:
            self._running = False
            self.ledger.set_kill_switch(True, actor=actor, reason=reason)
        self.notify_webhook("KILL SWITCH TRIGGERED", f"Actor: {actor} | Reason: {reason}")
        return {"ok": True, "kill_switch_active": True, "reason": reason}

    def reset_kill_switch(self, *, actor: str) -> dict[str, Any]:
        with self._lock:
            self.ledger.set_kill_switch(False, actor=actor, reason="operator_reset")
        return {"ok": True, "kill_switch_active": False}

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        requested_volume: Optional[float] = None,
        strategy_id: str = "manual",
        actor: str = "operator",
    ) -> tuple[RiskEvaluation, Optional[OrderReceipt]]:
        snap = self.broker.get_account_snapshot()
        sym = symbol.upper()
        quote = self.broker.get_quote(sym)
        if not snap.connected:
            ev = RiskEvaluation(False, "R_BROKER_DISCONNECTED", policy_hash=self.policy.policy_hash)
            return ev, None
        if quote is None:
            ev = RiskEvaluation(False, f"R_NO_QUOTE:{sym}", policy_hash=self.policy.policy_hash)
            return ev, None

        day_start, peak = self._update_equity_watermarks(snap.equity)
        open_positions = self.broker.get_open_positions()
        entry_px = quote.ask if side.upper() == "BUY" else quote.bid

        ev = self.risk_gate.evaluate_order(
            trade_mode=snap.trade_mode,
            symbol=sym,
            side=side,
            entry_price=entry_px,
            stop_loss=stop_loss,
            requested_volume=requested_volume,
            equity=snap.equity,
            day_start_equity=day_start,
            peak_equity=peak,
            open_positions_count=len(open_positions),
            spread_pips=quote.spread_pips,
            quote_age_sec=quote.age_sec,
            kill_switch_active=self.ledger.is_kill_switch_active(),
        )
        if not ev.approved:
            if ev.reason.startswith("R_DAILY_LOSS_LIMIT_BREACHED") or ev.reason.startswith("R_MAX_DRAWDOWN_BREACHED"):
                self.trigger_kill_switch(actor="risk_gate", reason=ev.reason)
            self.ledger.record_audit(
                actor=actor,
                action="ORDER_REJECTED_BY_RISK",
                details={"symbol": sym, "side": side, "reason": ev.reason, "strategy": strategy_id},
            )
            return ev, None

        client_order_id = f"x4-{uuid.uuid4().hex[:12]}"
        receipt = self.broker.place_market_order(
            client_order_id=client_order_id,
            symbol=sym,
            side=side.upper(),
            volume=ev.approved_volume,
            sl=stop_loss,
            tp=take_profit,
            comment=f"{strategy_id[:16]}",
        )
        self.ledger.record_order(
            client_order_id=client_order_id,
            broker_mode=self.broker.mode_name,
            symbol=sym,
            side=side.upper(),
            volume=ev.approved_volume,
            entry_price=entry_px,
            sl=stop_loss,
            tp=take_profit,
            status=receipt.status,
            broker_ticket=receipt.broker_ticket,
            fill_price=receipt.fill_price,
            strategy_id=strategy_id,
            policy_hash=self.policy.policy_hash,
            message=receipt.message,
        )
        self.ledger.record_audit(
            actor=actor,
            action="ORDER_SUBMITTED",
            details={
                "client_order_id": client_order_id,
                "symbol": sym,
                "side": side.upper(),
                "volume": ev.approved_volume,
                "status": receipt.status,
                "ticket": receipt.broker_ticket,
            },
        )
        return ev, receipt

    def run_once(self) -> None:
        self.broker.tick()
        snap = self.broker.get_account_snapshot()
        self._cycle_count += 1
        self._last_cycle_at = utc_now_iso()
        if not snap.connected:
            self._last_signal_summary = "Waiting for broker / MT5 bridge heartbeat..."
            return
        if self.ledger.is_kill_switch_active():
            self._running = False
            self._last_signal_summary = "Halted: Kill switch active."
            return

        day_start, peak = self._update_equity_watermarks(snap.equity)
        if day_start > 0 and ((day_start - snap.equity) / day_start) >= self.policy.risk.daily_loss_limit_pct:
            self.trigger_kill_switch(actor="supervisor", reason="Daily loss limit breached")
            return
        if peak > 0 and ((peak - snap.equity) / peak) >= self.policy.risk.max_overall_drawdown_pct:
            self.trigger_kill_switch(actor="supervisor", reason="Max drawdown limit breached")
            return

        open_syms = {p.symbol for p in self.broker.get_open_positions()}
        get_series = getattr(self.broker, "get_price_series", None)
        if not callable(get_series):
            return

        for sym in self.symbols:
            if sym in open_syms:
                continue
            closes = get_series(sym)
            sig = generate_signal(self.active_strategy, sym, closes)
            if sig is None:
                continue
            ev, receipt = self.submit_order(
                symbol=sig.symbol,
                side=sig.side,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                strategy_id=sig.strategy_id,
                actor="strategy_engine",
            )
            if ev.approved and receipt is not None:
                self._last_signal_summary = f"{sig.strategy_id}: {sig.side} {sig.symbol} ({receipt.status})"
                break
            self._last_signal_summary = f"{sig.symbol} signal blocked: {ev.reason}"

    def _loop(self) -> None:
        while self._running:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Supervisor cycle error: %s", exc)
                self._last_signal_summary = f"Cycle error: {type(exc).__name__}"
            time.sleep(self.settings.poll_interval_sec)

    def status_snapshot(self) -> dict[str, Any]:
        snap = self.broker.get_account_snapshot()
        day_start, peak = self._update_equity_watermarks(snap.equity) if snap.equity > 0 else (0.0, 0.0)
        daily_dd = round(max(0.0, (day_start - snap.equity) / day_start) * 100.0, 2) if day_start > 0 else 0.0
        overall_dd = round(max(0.0, (peak - snap.equity) / peak) * 100.0, 2) if peak > 0 else 0.0
        quotes = {
            k: {"bid": q.bid, "ask": q.ask, "spread_pips": q.spread_pips, "age_sec": q.age_sec}
            for k, q in self.broker.get_all_quotes().items()
        }
        bridge_diag = getattr(self.broker, "get_bridge_diagnostics", lambda: None)()
        return {
            "engine": {
                "running": self._running,
                "strategy": self.active_strategy,
                "available_strategies": sorted(STRATEGIES.keys()),
                "started_at": self._started_at,
                "last_cycle_at": self._last_cycle_at,
                "cycle_count": self._cycle_count,
                "last_summary": self._last_signal_summary,
                "kill_switch_active": self.ledger.is_kill_switch_active(),
                "kill_switch_reason": self.ledger.get_state("kill_switch_reason", ""),
            },
            "account": {
                "connected": snap.connected,
                "broker_mode": snap.broker_mode,
                "trade_mode": snap.trade_mode,
                "account_label": snap.account_label,
                "server": snap.server,
                "balance": snap.balance,
                "equity": snap.equity,
                "margin": snap.margin,
                "margin_free": snap.margin_free,
                "currency": snap.currency,
                "heartbeat_age_sec": snap.heartbeat_age_sec,
                "daily_drawdown_pct": daily_dd,
                "overall_drawdown_pct": overall_dd,
            },
            "policy": {
                "policy_id": self.policy.policy_id,
                "policy_version": self.policy.policy_version,
                "policy_hash": self.policy.policy_hash[:16],
                "allow_real": self.policy.allow_real,
                "real_execution_unlock": self.settings.real_execution_unlock,
                "max_risk_per_trade_pct": self.policy.risk.max_risk_per_trade_pct * 100.0,
                "daily_loss_limit_pct": self.policy.risk.daily_loss_limit_pct * 100.0,
                "max_overall_drawdown_pct": self.policy.risk.max_overall_drawdown_pct * 100.0,
                "max_open_positions": self.policy.risk.max_open_positions,
            },
            "bridge": bridge_diag,
            "quotes": quotes,
            "positions": [p.to_dict() for p in self.broker.get_open_positions()],
            "orders": self.ledger.list_orders(limit=30),
            "audit_chain_valid": self.ledger.verify_audit_chain(),
            "audit_recent": self.ledger.list_audit(limit=15),
        }
