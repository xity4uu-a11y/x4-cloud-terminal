"""Pluggable broker adapter protocol and domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class AccountSnapshot:
    connected: bool
    broker_mode: str  # paper | mt5_bridge | metaapi
    trade_mode: str   # paper | demo | real | unknown
    account_label: str
    server: str
    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float = 0.0
    currency: str = "USD"
    heartbeat_age_sec: Optional[float] = 0.0


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    bid: float
    ask: float
    spread_pips: float
    ts_epoch: float
    age_sec: float


@dataclass
class OpenPosition:
    ticket: int
    symbol: str
    side: str  # BUY | SELL
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    magic: int = 240400
    comment: str = ""
    opened_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "open_price": self.open_price,
            "current_price": self.current_price,
            "sl": self.sl,
            "tp": self.tp,
            "profit": self.profit,
            "magic": self.magic,
            "comment": self.comment,
            "opened_at": self.opened_at,
        }


@dataclass(frozen=True)
class OrderReceipt:
    status: str  # ACKED | QUEUED | REJECTED | ERROR
    client_order_id: str
    broker_ticket: Optional[int] = None
    fill_price: Optional[float] = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BrokerAdapter(Protocol):
    mode_name: str

    def get_account_snapshot(self) -> AccountSnapshot: ...
    def get_quote(self, symbol: str) -> Optional[MarketQuote]: ...
    def get_all_quotes(self) -> dict[str, MarketQuote]: ...
    def get_open_positions(self) -> list[OpenPosition]: ...
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
    ) -> OrderReceipt: ...
    def close_position(self, *, ticket: int, comment: str = "") -> OrderReceipt: ...
    def tick(self) -> None: ...
