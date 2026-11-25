"""Deterministic local trading strategies (zero external LLM API fees required)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from x4_cloud.policy import pip_size_for_symbol


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    symbol: str
    side: str  # BUY | SELL
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float
    rationale: str


def _ema(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema_val = float(values[0])
    for v in values[1:]:
        ema_val = (float(v) * k) + (ema_val * (1.0 - k))
    return ema_val


def _mean_abs_range(values: Sequence[float], period: int = 14) -> float:
    if len(values) < 2:
        return 0.0
    diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    window = diffs[-period:]
    return sum(window) / len(window) if window else 0.0


def _rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += -delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def evaluate_keltner_atr_breakout(symbol: str, closes: Sequence[float]) -> Optional[StrategySignal]:
    """Keltner Channel + volatility-normalized breakout strategy."""
    if len(closes) < 30:
        return None
    ema20 = _ema(closes[-30:], 20)
    ema50 = _ema(closes[-60:] if len(closes) >= 60 else closes, 50)
    atr = _mean_abs_range(closes, 14)
    pip = pip_size_for_symbol(symbol)
    min_stop = pip * 8.0
    if atr <= 0:
        return None

    last = float(closes[-1])
    prev = float(closes[-2])
    upper = ema20 + (1.6 * atr)
    lower = ema20 - (1.6 * atr)
    stop_dist = max(min_stop, 2.2 * atr)
    tp_dist = stop_dist * 1.8

    if last > upper and prev <= upper and ema20 >= ema50:
        return StrategySignal(
            strategy_id="keltner_atr_breakout",
            symbol=symbol.upper(),
            side="BUY",
            entry_price=last,
            stop_loss=round(last - stop_dist, 5),
            take_profit=round(last + tp_dist, 5),
            confidence=0.72,
            rationale=f"Close {last:.5f} broke above Keltner upper {upper:.5f} with uptrend EMA20>=EMA50.",
        )

    if last < lower and prev >= lower and ema20 <= ema50:
        return StrategySignal(
            strategy_id="keltner_atr_breakout",
            symbol=symbol.upper(),
            side="SELL",
            entry_price=last,
            stop_loss=round(last + stop_dist, 5),
            take_profit=round(last - tp_dist, 5),
            confidence=0.72,
            rationale=f"Close {last:.5f} broke below Keltner lower {lower:.5f} with downtrend EMA20<=EMA50.",
        )
    return None


def evaluate_ema_rsi_pullback(symbol: str, closes: Sequence[float]) -> Optional[StrategySignal]:
    """Dual-EMA trend direction + RSI pullback continuation."""
    if len(closes) < 35:
        return None
    ema_fast = _ema(closes[-25:], 12)
    ema_slow = _ema(closes[-50:] if len(closes) >= 50 else closes, 34)
    rsi = _rsi(closes, 14)
    atr = _mean_abs_range(closes, 14)
    pip = pip_size_for_symbol(symbol)
    stop_dist = max(pip * 8.0, 2.0 * atr)
    tp_dist = stop_dist * 1.75
    last = float(closes[-1])

    if ema_fast > ema_slow and rsi <= 34.0:
        return StrategySignal(
            strategy_id="ema_rsi_pullback",
            symbol=symbol.upper(),
            side="BUY",
            entry_price=last,
            stop_loss=round(last - stop_dist, 5),
            take_profit=round(last + tp_dist, 5),
            confidence=0.70,
            rationale=f"Uptrend pullback RSI={rsi:.1f} <= 34 with EMA12>EMA34.",
        )

    if ema_fast < ema_slow and rsi >= 66.0:
        return StrategySignal(
            strategy_id="ema_rsi_pullback",
            symbol=symbol.upper(),
            side="SELL",
            entry_price=last,
            stop_loss=round(last + stop_dist, 5),
            take_profit=round(last - tp_dist, 5),
            confidence=0.70,
            rationale=f"Downtrend pullback RSI={rsi:.1f} >= 66 with EMA12<EMA34.",
        )
    return None


STRATEGIES = {
    "keltner_atr_breakout": evaluate_keltner_atr_breakout,
    "ema_rsi_pullback": evaluate_ema_rsi_pullback,
}


def generate_signal(strategy_id: str, symbol: str, closes: Sequence[float]) -> Optional[StrategySignal]:
    fn = STRATEGIES.get(strategy_id, evaluate_keltner_atr_breakout)
    return fn(symbol, closes)
