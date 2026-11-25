"""Cross-platform AES-256-GCM + scrypt secret vault (Linux / macOS / Windows / Docker).

Replaces Windows-only DPAPI (`CryptProtectData`) so X4 Cloud Terminal can run
securely inside Linux containers and free PaaS environments without OS lock-in.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_AAD_PREFIX = b"X4_CLOUD_VAULT_V1:"
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*Bearer)\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*([^\s,'\"]+)"),
)


def redact_secrets(text: str) -> str:
    """Scrub likely tokens/passwords/keys from log strings before emission."""
    if not text:
        return ""
    masked = _SECRET_PATTERNS[0].sub(r"\1 [REDACTED]", text)
    masked = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", masked)
    masked = _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)}=[REDACTED]", masked)
    return masked


class CrossPlatformVault:
    """File-backed AES-256-GCM secret store using scrypt key derivation.

    Each stored key gets a fresh random 16-byte salt and 12-byte nonce, with the
    key name bound as Additional Authenticated Data (AAD) to prevent ciphertext
    transplantation across keys.
    """

    def __init__(self, vault_path: Path, passphrase: str, *, create_if_missing: bool = True) -> None:
        if not passphrase or len(passphrase) < 8:
            raise ValueError("Vault passphrase must be at least 8 characters long.")
        self._path = Path(vault_path)
        self._passphrase = passphrase.encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if create_if_missing and not self._path.exists():
            self._save_raw({"version": 1, "secrets": {}})

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
        return kdf.derive(passphrase)

    def _load_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "secrets": {}}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
            raise ValueError("Corrupted vault file format.")
        return data

    def _save_raw(self, payload: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        tmp.replace(self._path)

    def set_secret(self, key: str, value: str) -> None:
        clean_key = (key or "").strip()
        if not clean_key:
            raise ValueError("Secret key must be non-empty.")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        aes_key = self._derive_key(self._passphrase, salt)
        aad = _AAD_PREFIX + clean_key.encode("utf-8")
        ct = AESGCM(aes_key).encrypt(nonce, value.encode("utf-8"), aad)

        data = self._load_raw()
        data["secrets"][clean_key] = {
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ct).decode("ascii"),
        }
        self._save_raw(data)

    def get_secret(self, key: str) -> Optional[str]:
        clean_key = (key or "").strip()
        if not clean_key:
            return None
        data = self._load_raw()
        entry = data["secrets"].get(clean_key)
        if not entry:
            return None
        try:
            salt = base64.b64decode(entry["salt"])
            nonce = base64.b64decode(entry["nonce"])
            ct = base64.b64decode(entry["ciphertext"])
            aes_key = self._derive_key(self._passphrase, salt)
            aad = _AAD_PREFIX + clean_key.encode("utf-8")
            pt = AESGCM(aes_key).decrypt(nonce, ct, aad)
            return pt.decode("utf-8")
        except (KeyError, ValueError, InvalidTag) as exc:
            raise PermissionError(f"Unable to decrypt vault secret '{clean_key}': invalid passphrase or tampered entry.") from exc

    def delete_secret(self, key: str) -> bool:
        data = self._load_raw()
        if key not in data["secrets"]:
            return False
        del data["secrets"][key]
        self._save_raw(data)
        return True

    def list_keys(self) -> list[str]:
        return sorted(self._load_raw()["secrets"].keys())
