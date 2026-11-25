"""Optional MetaApi Cloud REST broker adapter (pure HTTP, zero local MT5 terminal required)."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from x4_cloud.brokers import AccountSnapshot, MarketQuote, OpenPosition, OrderReceipt
from x4_cloud.policy import pip_size_for_symbol


class MetaApiBrokerAdapter:
    """Connects X4 Cloud Terminal directly to a MetaApi-hosted MT5 account via REST."""

    mode_name = "metaapi"

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        region: str = "vint-hill",
        timeout_sec: float = 10.0,
    ) -> None:
        self._token = token.strip()
        self._account_id = account_id.strip()
        self._base_url = f"https://mt-client-api-v1.{region}.agiliumtrade.ai"
        self._timeout = float(timeout_sec)
        self._quotes_cache: dict[str, MarketQuote] = {}

    def _headers(self) -> dict[str, str]:
        return {"auth-token": self._token, "Accept": "application/json"}

    def _is_configured(self) -> bool:
        return bool(self._token and self._account_id)

    def _request(self, method: str, path: str, json_body: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self._base_url}/users/current/accounts/{self._account_id}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, url, headers=self._headers(), json=json_body)
            resp.raise_for_status()
            return resp.json()

    def tick(self) -> None:
        return None

    def get_account_snapshot(self) -> AccountSnapshot:
        if not self._is_configured():
            return AccountSnapshot(
                connected=False,
                broker_mode=self.mode_name,
                trade_mode="unknown",
                account_label="METAAPI_UNCONFIGURED",
                server="missing-credentials",
                balance=0.0,
                equity=0.0,
                heartbeat_age_sec=None,
            )
        try:
            info = self._request("GET", "/account-information")
            raw_type = str(info.get("type", "")).lower()
            trade_mode = "demo" if "demo" in raw_type else ("real" if "real" in raw_type else "unknown")
            return AccountSnapshot(
                connected=True,
                broker_mode=self.mode_name,
                trade_mode=trade_mode,
                account_label=str(info.get("login", self._account_id[:8])),
                server=str(info.get("server", "MetaApi")),
                balance=float(info.get("balance", 0.0)),
                equity=float(info.get("equity", 0.0)),
                margin=float(info.get("margin", 0.0)),
                margin_free=float(info.get("freeMargin", 0.0)),
                currency=str(info.get("currency", "USD")),
                heartbeat_age_sec=0.0,
            )
        except Exception:
            return AccountSnapshot(
                connected=False,
                broker_mode=self.mode_name,
                trade_mode="unknown",
                account_label=self._account_id[:8],
                server="metaapi-unreachable",
                balance=0.0,
                equity=0.0,
                heartbeat_age_sec=None,
            )

    def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        if not self._is_configured():
            return None
        sym = symbol.upper()
        try:
            data = self._request("GET", f"/symbols/{sym}/current-price")
            bid = float(data["bid"])
            ask = float(data["ask"])
            pip = pip_size_for_symbol(sym)
            q = MarketQuote(sym, bid, ask, round((ask - bid) / pip, 2), time.time(), 0.0)
            self._quotes_cache[sym] = q
            return q
        except Exception:
            return self._quotes_cache.get(sym)

    def get_all_quotes(self) -> dict[str, MarketQuote]:
        return dict(self._quotes_cache)

    def get_open_positions(self) -> list[OpenPosition]:
        if not self._is_configured():
            return []
        try:
            rows = self._request("GET", "/positions") or []
            out: list[OpenPosition] = []
            for r in rows:
                side = "BUY" if "BUY" in str(r.get("type", "")).upper() else "SELL"
                out.append(
                    OpenPosition(
                        ticket=int(r.get("id", 0)),
                        symbol=str(r.get("symbol", "")).upper(),
                        side=side,
                        volume=float(r.get("volume", 0.0)),
                        open_price=float(r.get("openPrice", 0.0)),
                        current_price=float(r.get("currentPrice", r.get("openPrice", 0.0))),
                        sl=float(r.get("stopLoss", 0.0) or 0.0),
                        tp=float(r.get("takeProfit", 0.0) or 0.0),
                        profit=float(r.get("profit", 0.0) or 0.0),
                        magic=int(r.get("magic", 0) or 0),
                        comment=str(r.get("comment", "") or ""),
                        opened_at=str(r.get("time", "") or ""),
                    )
                )
            return out
        except Exception:
            return []

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
        if not self._is_configured():
            return OrderReceipt("REJECTED", client_order_id, message="MetaApi credentials missing")
        action_type = "ORDER_TYPE_BUY" if side.upper() == "BUY" else "ORDER_TYPE_SELL"
        payload: dict[str, Any] = {
            "actionType": action_type,
            "symbol": symbol.upper(),
            "volume": round(float(volume), 2),
            "comment": comment or client_order_id[:24],
            "clientId": client_order_id[:32],
        }
        if sl:
            payload["stopLoss"] = float(sl)
        if tp:
            payload["takeProfit"] = float(tp)
        try:
            res = self._request("POST", "/trade", json_body=payload)
            code = str(res.get("stringCode", ""))
            ok = code in {"TRADE_RETCODE_DONE", "TRADE_RETCODE_DONE_PARTIAL"}
            return OrderReceipt(
                status="ACKED" if ok else "REJECTED",
                client_order_id=client_order_id,
                broker_ticket=int(res["positionId"]) if res.get("positionId") else None,
                message=str(res.get("message", code)),
            )
        except Exception as exc:
            return OrderReceipt("ERROR", client_order_id, message=f"MetaApi trade error: {type(exc).__name__}")

    def close_position(self, *, ticket: int, comment: str = "") -> OrderReceipt:
        if not self._is_configured():
            return OrderReceipt("REJECTED", f"close-{ticket}", message="MetaApi credentials missing")
        try:
            res = self._request(
                "POST",
                "/trade",
                json_body={"actionType": "POSITION_CLOSE_ID", "positionId": str(ticket), "comment": comment},
            )
            code = str(res.get("stringCode", ""))
            return OrderReceipt(
                status="ACKED" if "DONE" in code else "REJECTED",
                client_order_id=f"close-{ticket}",
                broker_ticket=int(ticket),
                message=str(res.get("message", code)),
            )
        except Exception as exc:
            return OrderReceipt("ERROR", f"close-{ticket}", message=f"MetaApi close error: {type(exc).__name__}")
