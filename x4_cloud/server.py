"""Starlette + Uvicorn HTTP backend for X4 Cloud Terminal."""

from __future__ import annotations

import contextlib
import hmac
import json
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from x4_cloud import __version__
from x4_cloud.brokers import BrokerAdapter
from x4_cloud.brokers.metaapi_adapter import MetaApiBrokerAdapter
from x4_cloud.brokers.mt5_bridge import MT5BridgeBrokerAdapter, verify_bridge_signature
from x4_cloud.brokers.paper import PaperBrokerAdapter
from x4_cloud.config import CloudSettings
from x4_cloud.ledger import ExecutionLedger, utc_now_iso
from x4_cloud.policy import load_policy
from x4_cloud.supervisor import CloudTradingSupervisor

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _build_broker(settings: CloudSettings, ledger: ExecutionLedger) -> BrokerAdapter:
    if settings.broker_mode == "mt5_bridge":
        return MT5BridgeBrokerAdapter(ledger=ledger)
    if settings.broker_mode == "metaapi":
        return MetaApiBrokerAdapter(
            token=settings.metaapi_token,
            account_id=settings.metaapi_account_id,
            region=settings.metaapi_region,
        )
    return PaperBrokerAdapter(initial_balance=settings.paper_initial_balance, symbols=settings.symbols)


def create_app(
    settings: Optional[CloudSettings] = None,
    *,
    broker_override: Optional[BrokerAdapter] = None,
) -> Starlette:
    cfg = settings if settings is not None else CloudSettings.from_env()
    policy = load_policy(cfg.policy_path)
    ledger = ExecutionLedger(cfg.data_dir / "x4_cloud.db")
    broker = broker_override if broker_override is not None else _build_broker(cfg, ledger)
    supervisor = CloudTradingSupervisor(settings=cfg, policy=policy, ledger=ledger, broker=broker)

    def _is_operator_authorized(request: Request) -> bool:
        configured = cfg.api_token
        if not configured:
            return True
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.headers.get("x-x4-token", "").strip()
        return bool(token) and hmac.compare_digest(token, configured)

    def _auth_failed() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "Unauthorized. Provide Bearer X4_API_TOKEN."}, status_code=401)

    async def index(_: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    async def healthz(_: Request) -> JSONResponse:
        snap = broker.get_account_snapshot()
        return JSONResponse(
            {
                "ok": True,
                "service": "x4-cloud-terminal",
                "version": __version__,
                "broker_mode": broker.mode_name,
                "broker_connected": snap.connected,
                "utc": utc_now_iso(),
            }
        )

    async def api_status(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        return JSONResponse(supervisor.status_snapshot())

    async def api_engine_start(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        body = await _safe_json(request)
        strategy = body.get("strategy") if isinstance(body, dict) else None
        res = supervisor.start(actor="api_operator", strategy=strategy)
        return JSONResponse(res, status_code=200 if res.get("ok") else 409)

    async def api_engine_stop(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        return JSONResponse(supervisor.stop(actor="api_operator"))

    async def api_kill_switch(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        body = await _safe_json(request)
        active = bool(body.get("active", True))
        reason = str(body.get("reason", "Manual operator emergency halt")).strip() or "Manual emergency halt"
        if active:
            return JSONResponse(supervisor.trigger_kill_switch(actor="api_operator", reason=reason))
        return JSONResponse(supervisor.reset_kill_switch(actor="api_operator"))

    async def api_place_order(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        body = await _safe_json(request)
        try:
            symbol = str(body["symbol"]).strip().upper()
            side = str(body["side"]).strip().upper()
            sl = float(body["sl"]) if body.get("sl") not in (None, "") else None
            tp = float(body["tp"]) if body.get("tp") not in (None, "") else None
            vol = float(body["volume"]) if body.get("volume") not in (None, "") else None
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": f"Invalid order payload: {exc}"}, status_code=400)

        ev, receipt = supervisor.submit_order(
            symbol=symbol,
            side=side,
            stop_loss=sl,
            take_profit=tp,
            requested_volume=vol,
            strategy_id="manual_ticket",
            actor="api_operator",
        )
        if not ev.approved:
            return JSONResponse(
                {"ok": False, "approved": False, "reason": ev.reason, "policy_hash": ev.policy_hash[:16]},
                status_code=422,
            )
        return JSONResponse(
            {
                "ok": True,
                "approved": True,
                "approved_volume": ev.approved_volume,
                "estimated_risk_usd": ev.estimated_risk_usd,
                "receipt": {
                    "status": receipt.status if receipt else "UNKNOWN",
                    "client_order_id": receipt.client_order_id if receipt else "",
                    "broker_ticket": receipt.broker_ticket if receipt else None,
                    "fill_price": receipt.fill_price if receipt else None,
                    "message": receipt.message if receipt else "",
                },
            }
        )

    async def api_close_position(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        ticket = int(request.path_params["ticket"])
        receipt = broker.close_position(ticket=ticket, comment="X4_UI_CLOSE")
        ledger.record_audit(
            actor="api_operator",
            action="CLOSE_POSITION",
            details={"ticket": ticket, "status": receipt.status, "message": receipt.message},
        )
        ok = receipt.status in {"ACKED", "QUEUED"}
        return JSONResponse(
            {"ok": ok, "status": receipt.status, "ticket": ticket, "message": receipt.message},
            status_code=200 if ok else 404,
        )

    async def api_bridge_sync(request: Request) -> JSONResponse:
        if not isinstance(broker, MT5BridgeBrokerAdapter):
            return JSONResponse({"ok": False, "error": "Server is not running in mt5_bridge mode."}, status_code=400)
        raw = await request.body()
        ok_sig, reason = verify_bridge_signature(
            secret=cfg.bridge_secret,
            timestamp=request.headers.get("x-x4-timestamp", ""),
            signature=request.headers.get("x-x4-signature", ""),
            raw_body=raw,
        )
        if not ok_sig:
            return JSONResponse({"ok": False, "error": reason}, status_code=401)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        return JSONResponse(broker.ingest_sync(payload))

    async def api_bridge_ack(request: Request) -> JSONResponse:
        if not isinstance(broker, MT5BridgeBrokerAdapter):
            return JSONResponse({"ok": False, "error": "Server is not running in mt5_bridge mode."}, status_code=400)
        raw = await request.body()
        ok_sig, reason = verify_bridge_signature(
            secret=cfg.bridge_secret,
            timestamp=request.headers.get("x-x4-timestamp", ""),
            signature=request.headers.get("x-x4-signature", ""),
            raw_body=raw,
        )
        if not ok_sig:
            return JSONResponse({"ok": False, "error": reason}, status_code=401)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        updated = broker.ingest_ack(payload)
        return JSONResponse({"ok": updated})

    async def api_bridge_commands(request: Request) -> JSONResponse:
        if not _is_operator_authorized(request):
            return _auth_failed()
        return JSONResponse({"commands": ledger.list_bridge_commands(limit=30)})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if cfg.auto_start_engine and not ledger.is_kill_switch_active():
            supervisor.start(actor="auto_boot")
        try:
            yield
        finally:
            supervisor.stop(actor="shutdown", reason="server_shutdown")
            ledger.close()

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/status", api_status, methods=["GET"]),
        Route("/api/engine/start", api_engine_start, methods=["POST"]),
        Route("/api/engine/stop", api_engine_stop, methods=["POST"]),
        Route("/api/engine/kill-switch", api_kill_switch, methods=["POST"]),
        Route("/api/orders", api_place_order, methods=["POST"]),
        Route("/api/positions/{ticket:int}/close", api_close_position, methods=["POST"]),
        Route("/api/bridge/sync", api_bridge_sync, methods=["POST"]),
        Route("/api/bridge/ack", api_bridge_ack, methods=["POST"]),
        Route("/api/bridge/commands", api_bridge_commands, methods=["GET"]),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.settings = cfg
    app.state.supervisor = supervisor
    app.state.ledger = ledger
    app.state.broker = broker
    return app


async def _safe_json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
