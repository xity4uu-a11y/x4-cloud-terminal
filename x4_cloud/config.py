"""Central cross-platform runtime settings for X4 Cloud Terminal."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_symbols(raw: str | None) -> Tuple[str, ...]:
    if not raw:
        return ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY")
    items = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return tuple(items) if items else ("XAUUSD", "EURUSD")


@dataclass(frozen=True)
class CloudSettings:
    host: str = "0.0.0.0"
    port: int = 8501
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    policy_path: Path = field(default_factory=lambda: PROJECT_ROOT / "policies" / "demo_default.yaml")
    broker_mode: str = "paper"  # paper | mt5_bridge | metaapi
    api_token: str = ""
    bridge_secret: str = ""
    vault_passphrase: str = ""
    default_strategy: str = "keltner_atr_breakout"
    symbols: Tuple[str, ...] = ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY")
    poll_interval_sec: float = 5.0
    auto_start_engine: bool = False
    real_execution_unlock: bool = False
    paper_initial_balance: float = 10000.0
    metaapi_token: str = ""
    metaapi_account_id: str = ""
    metaapi_region: str = "vint-hill"
    alert_webhook_url: str = ""

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "CloudSettings":
        target_env = env_file if env_file is not None else PROJECT_ROOT / ".env"
        if target_env.exists():
            load_dotenv(target_env, override=False)

        data_raw = os.getenv("X4_DATA_DIR", "").strip()
        data_dir = Path(data_raw).resolve() if data_raw else (PROJECT_ROOT / "data")

        policy_raw = os.getenv("X4_POLICY_PATH", "").strip()
        policy_path = Path(policy_raw).resolve() if policy_raw else (PROJECT_ROOT / "policies" / "demo_default.yaml")

        broker_mode = os.getenv("X4_BROKER_MODE", "paper").strip().lower()
        if broker_mode not in {"paper", "mt5_bridge", "metaapi"}:
            broker_mode = "paper"

        try:
            port = int(os.getenv("PORT", "8501"))
        except ValueError:
            port = 8501

        try:
            poll = max(1.0, float(os.getenv("X4_POLL_INTERVAL_SEC", "5.0")))
        except ValueError:
            poll = 5.0

        try:
            paper_balance = max(100.0, float(os.getenv("X4_PAPER_INITIAL_BALANCE", "10000.0")))
        except ValueError:
            paper_balance = 10000.0

        return cls(
            host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=port,
            data_dir=data_dir,
            policy_path=policy_path,
            broker_mode=broker_mode,
            api_token=os.getenv("X4_API_TOKEN", "").strip(),
            bridge_secret=os.getenv("X4_BRIDGE_SECRET", "").strip(),
            vault_passphrase=os.getenv("X4_VAULT_PASSPHRASE", "").strip(),
            default_strategy=os.getenv("X4_DEFAULT_STRATEGY", "keltner_atr_breakout").strip() or "keltner_atr_breakout",
            symbols=_parse_symbols(os.getenv("X4_SYMBOLS")),
            poll_interval_sec=poll,
            auto_start_engine=_parse_bool(os.getenv("X4_AUTO_START_ENGINE"), default=False),
            real_execution_unlock=_parse_bool(os.getenv("REAL_EXECUTION_UNLOCK"), default=False),
            paper_initial_balance=paper_balance,
            metaapi_token=os.getenv("METAAPI_TOKEN", "").strip(),
            metaapi_account_id=os.getenv("METAAPI_ACCOUNT_ID", "").strip(),
            metaapi_region=os.getenv("METAAPI_REGION", "vint-hill").strip() or "vint-hill",
            alert_webhook_url=os.getenv("X4_ALERT_WEBHOOK_URL", "").strip(),
        )
