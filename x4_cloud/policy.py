"""Fail-closed risk & execution policy engine with SHA-256 policy verification."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_pct: float
    daily_loss_limit_pct: float
    max_overall_drawdown_pct: float
    max_open_positions: int
    max_lot_size: float
    min_lot_size: float
    max_spread_pips: float
    max_quote_age_sec: float
    min_stop_distance_pips: float
    require_stop_loss: bool = True


@dataclass(frozen=True)
class TradingPolicy:
    policy_id: str
    policy_version: str
    policy_hash: str
    description: str
    allow_real: bool
    allowed_modes: tuple[str, ...]
    allowed_symbols: tuple[str, ...]
    risk: RiskLimits


@dataclass(frozen=True)
class RiskEvaluation:
    approved: bool
    reason: str
    approved_volume: float = 0.0
    estimated_risk_usd: float = 0.0
    policy_hash: str = ""


def load_policy(policy_path: Path) -> TradingPolicy:
    path = Path(policy_path)
    if not path.exists():
        raise FileNotFoundError(f"Policy file not found: {path}")
    raw_bytes = path.read_bytes()
    policy_hash = hashlib.sha256(raw_bytes).hexdigest()
    doc = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    r = doc.get("risk") or {}

    risk = RiskLimits(
        max_risk_per_trade_pct=float(r.get("max_risk_per_trade_pct", 0.005)),
        daily_loss_limit_pct=float(r.get("daily_loss_limit_pct", 0.02)),
        max_overall_drawdown_pct=float(r.get("max_overall_drawdown_pct", 0.08)),
        max_open_positions=int(r.get("max_open_positions", 3)),
        max_lot_size=float(r.get("max_lot_size", 0.50)),
        min_lot_size=float(r.get("min_lot_size", 0.01)),
        max_spread_pips=float(r.get("max_spread_pips", 4.5)),
        max_quote_age_sec=float(r.get("max_quote_age_sec", 90.0)),
        min_stop_distance_pips=float(r.get("min_stop_distance_pips", 5.0)),
        require_stop_loss=bool(r.get("require_stop_loss", True)),
    )

    return TradingPolicy(
        policy_id=str(doc.get("policy_id", "x4_default")),
        policy_version=str(doc.get("policy_version", "1.0.0")),
        policy_hash=policy_hash,
        description=str(doc.get("description", "")),
        allow_real=bool(doc.get("allow_real", False)),
        allowed_modes=tuple(str(m).strip().lower() for m in (doc.get("allowed_modes") or ["paper", "demo"])),
        allowed_symbols=tuple(str(s).strip().upper() for s in (doc.get("allowed_symbols") or [])),
        risk=risk,
    )


def pip_size_for_symbol(symbol: str) -> float:
    sym = symbol.upper()
    if "XAU" in sym or "GOLD" in sym:
        return 0.10
    if "JPY" in sym:
        return 0.01
    if any(k in sym for k in ("BTC", "ETH", "US30", "NAS100", "USTEC")):
        return 1.0
    return 0.0001


def pip_value_per_lot_usd(symbol: str, price: float) -> float:
    """Conservative USD pip value per 1.00 standard lot for sizing."""
    sym = symbol.upper()
    if "XAU" in sym or "GOLD" in sym:
        return 10.0  # 100 oz * $0.10 pip
    if sym.endswith("USD") or sym.endswith("USDM"):
        return 10.0
    if "JPY" in sym and price > 0:
        return round((1000.0 / price), 4)
    return 10.0


class RiskGate:
    """Deterministic fail-closed pre-trade risk validator."""

    def __init__(self, policy: TradingPolicy, *, real_execution_unlock: bool = False) -> None:
        self.policy = policy
        self.real_execution_unlock = bool(real_execution_unlock)

    def evaluate_order(
        self,
        *,
        trade_mode: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: Optional[float],
        requested_volume: Optional[float],
        equity: float,
        day_start_equity: float,
        peak_equity: float,
        open_positions_count: int,
        spread_pips: float,
        quote_age_sec: Optional[float],
        kill_switch_active: bool,
    ) -> RiskEvaluation:
        p = self.policy
        r = p.risk
        mode = (trade_mode or "unknown").strip().lower()
        sym = (symbol or "").strip().upper()
        dir_side = (side or "").strip().upper()

        if kill_switch_active:
            return RiskEvaluation(False, "R_KILL_SWITCH_ACTIVE", policy_hash=p.policy_hash)

        if mode == "real":
            if not (p.allow_real and self.real_execution_unlock):
                return RiskEvaluation(False, "R_REAL_MODE_DOUBLE_LOCKED", policy_hash=p.policy_hash)
        elif mode not in p.allowed_modes:
            return RiskEvaluation(False, f"R_TRADE_MODE_BLOCKED:{mode}", policy_hash=p.policy_hash)

        if dir_side not in {"BUY", "SELL"}:
            return RiskEvaluation(False, f"R_INVALID_SIDE:{dir_side}", policy_hash=p.policy_hash)

        if p.allowed_symbols and sym not in p.allowed_symbols:
            return RiskEvaluation(False, f"R_SYMBOL_NOT_ALLOWED:{sym}", policy_hash=p.policy_hash)

        if not (math.isfinite(equity) and equity > 0 and math.isfinite(entry_price) and entry_price > 0):
            return RiskEvaluation(False, "R_INVALID_EQUITY_OR_PRICE", policy_hash=p.policy_hash)

        if day_start_equity > 0:
            daily_dd = max(0.0, (day_start_equity - equity) / day_start_equity)
            if daily_dd >= r.daily_loss_limit_pct:
                return RiskEvaluation(False, f"R_DAILY_LOSS_LIMIT_BREACHED:{daily_dd:.4f}", policy_hash=p.policy_hash)

        if peak_equity > 0:
            overall_dd = max(0.0, (peak_equity - equity) / peak_equity)
            if overall_dd >= r.max_overall_drawdown_pct:
                return RiskEvaluation(False, f"R_MAX_DRAWDOWN_BREACHED:{overall_dd:.4f}", policy_hash=p.policy_hash)

        if open_positions_count >= r.max_open_positions:
            return RiskEvaluation(False, f"R_MAX_OPEN_POSITIONS:{open_positions_count}>={r.max_open_positions}", policy_hash=p.policy_hash)

        if not math.isfinite(spread_pips) or spread_pips < 0 or spread_pips > r.max_spread_pips:
            return RiskEvaluation(False, f"R_SPREAD_EXCEEDS_MAX:{spread_pips}", policy_hash=p.policy_hash)

        if quote_age_sec is None or not math.isfinite(quote_age_sec) or quote_age_sec < -5.0 or quote_age_sec > r.max_quote_age_sec:
            return RiskEvaluation(False, f"R_STALE_QUOTE:{quote_age_sec}", policy_hash=p.policy_hash)

        if r.require_stop_loss:
            if stop_loss is None or not math.isfinite(stop_loss) or stop_loss <= 0:
                return RiskEvaluation(False, "R_STOP_LOSS_REQUIRED", policy_hash=p.policy_hash)
            if dir_side == "BUY" and stop_loss >= entry_price:
                return RiskEvaluation(False, "R_INVALID_BUY_STOP_LOSS", policy_hash=p.policy_hash)
            if dir_side == "SELL" and stop_loss <= entry_price:
                return RiskEvaluation(False, "R_INVALID_SELL_STOP_LOSS", policy_hash=p.policy_hash)

        pip = pip_size_for_symbol(sym)
        stop_pips = abs(entry_price - float(stop_loss)) / pip if stop_loss else r.min_stop_distance_pips
        if stop_pips < r.min_stop_distance_pips:
            return RiskEvaluation(False, f"R_STOP_TOO_TIGHT:{stop_pips:.2f}pips", policy_hash=p.policy_hash)

        max_risk_usd = equity * r.max_risk_per_trade_pct
        pip_val = pip_value_per_lot_usd(sym, entry_price)
        risk_per_lot = stop_pips * pip_val
        if risk_per_lot <= 0:
            return RiskEvaluation(False, "R_INVALID_RISK_PER_LOT", policy_hash=p.policy_hash)

        max_allowed_vol = math.floor((max_risk_usd / risk_per_lot) * 100.0) / 100.0
        max_allowed_vol = min(max_allowed_vol, r.max_lot_size)
        if max_allowed_vol < r.min_lot_size:
            return RiskEvaluation(False, "R_MIN_LOT_EXCEEDS_RISK_CAP", policy_hash=p.policy_hash)

        if requested_volume is None:
            final_vol = max_allowed_vol
        else:
            req = math.floor(float(requested_volume) * 100.0) / 100.0
            if req < r.min_lot_size:
                return RiskEvaluation(False, f"R_VOLUME_BELOW_MIN:{req}", policy_hash=p.policy_hash)
            final_vol = min(req, max_allowed_vol)

        est_risk = round(final_vol * risk_per_lot, 2)
        return RiskEvaluation(
            approved=True,
            reason="APPROVED",
            approved_volume=round(final_vol, 2),
            estimated_risk_usd=est_risk,
            policy_hash=p.policy_hash,
        )


def check_symbols_whitelist(symbols: Iterable[str], policy: TradingPolicy) -> list[str]:
    if not policy.allowed_symbols:
        return [s.upper() for s in symbols]
    allowed = set(policy.allowed_symbols)
    return [s.upper() for s in symbols if s.upper() in allowed]
