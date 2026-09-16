#!/usr/bin/env python3
"""
secret_store.py — Provider secret storage (autoroute spec §10, §50, §51, §62).

Hard rules implemented here:

* ProviderConfig != ProviderSecret. Provider settings live in
  ~/.context-agent/providers.json; secrets NEVER do.
* On Windows secrets are encrypted with DPAPI (CryptProtectData). Elsewhere
  they are obfuscated with a machine-local key; the storage method is
  reported honestly ("dpapi" vs "obfuscated").
* The raw key is never returned by any listing/JSON output: only a mask
  (first 3 + last 4 chars). Replace and delete are supported.
* Secrets never enter context output, ledger, memory, prompts, traces,
  usage records, diagnostics, or dashboard GET responses.
* When the Router is disabled there is no code path that reads secrets.

Usage:
  python secret_store.py --list
  python secret_store.py --set PROVIDER_ID     (key from SECRET_VALUE env or stdin)
  python secret_store.py --delete PROVIDER_ID
  python secret_store.py --has PROVIDER_ID
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import getpass
import hashlib
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def secrets_path() -> Path:
    return Path.home() / ".context-agent" / "secrets.json"


def _load() -> dict:
    path = secrets_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    try:
        if platform.system() != "Windows":
            os.chmod(path, 0o600)
    except Exception:
        pass


def mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 7:
        return "***"
    return f"{text[:3]}...{text[-4:]}"


# ── DPAPI (Windows) ────────────────────────────────────────────────────


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_protect(plain: bytes) -> bytes | None:
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = _DATA_BLOB(len(plain), ctypes.create_string_buffer(plain, len(plain)))
        blob_out = _DATA_BLOB()
        if crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None,
                                    None, 0, ctypes.byref(blob_out)):
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return data
    except Exception:
        pass
    return None


def _dpapi_unprotect(cipher: bytes) -> bytes | None:
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = _DATA_BLOB(len(cipher), ctypes.create_string_buffer(cipher, len(cipher)))
        blob_out = _DATA_BLOB()
        if crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                      None, 0, ctypes.byref(blob_out)):
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return data
    except Exception:
        pass
    return None


def _obfuscation_key() -> bytes:
    material = f"{platform.node()}|{os.environ.get('USERNAME', os.environ.get('USER', 'u'))}|context-agent-secrets"
    return hashlib.sha256(material.encode("utf-8")).digest()


def _obfuscate(plain: bytes) -> bytes:
    key = _obfuscation_key()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(plain))


def _encrypt(value: str) -> tuple[str, str]:
    raw = value.encode("utf-8")
    if platform.system() == "Windows":
        protected = _dpapi_protect(raw)
        if protected is not None:
            return base64.b64encode(protected).decode("ascii"), "dpapi"
    return base64.b64encode(_obfuscate(raw)).decode("ascii"), "obfuscated"


def _decrypt(payload: str, method: str) -> str | None:
    try:
        raw = base64.b64decode(payload.encode("ascii"))
    except Exception:
        return None
    if method == "dpapi":
        plain = _dpapi_unprotect(raw)
        return plain.decode("utf-8") if plain is not None else None
    if method == "obfuscated":
        return _obfuscate(raw).decode("utf-8")
    return None


# ── API ────────────────────────────────────────────────────────────────


def set_secret(provider_id: str, api_key: str) -> dict:
    provider_id = str(provider_id or "").strip().lower()
    api_key = str(api_key or "").strip()
    if not provider_id:
        return {"error": "provider_id_required"}
    if not api_key:
        return {"error": "api_key_required"}
    data = _load()
    payload, method = _encrypt(api_key)
    existed = provider_id in data
    data[provider_id] = {
        "encrypted": payload,
        "method": method,
        "mask": mask_secret(api_key),
        "updated_at": datetime.now().isoformat(),
    }
    _save(data)
    return {"ok": True, "provider_id": provider_id, "mask": mask_secret(api_key),
            "method": method, "action": "replaced" if existed else "created"}


def has_secret(provider_id: str) -> bool:
    return str(provider_id or "").strip().lower() in _load()


def get_secret(provider_id: str) -> str | None:
    """Internal only. Callers must never serialize the returned value."""
    entry = _load().get(str(provider_id or "").strip().lower())
    if not entry:
        return None
    return _decrypt(entry.get("encrypted", ""), entry.get("method", ""))


def delete_secret(provider_id: str) -> dict:
    provider_id = str(provider_id or "").strip().lower()
    data = _load()
    if provider_id not in data:
        return {"ok": False, "error": "not_found"}
    del data[provider_id]
    _save(data)
    return {"ok": True, "provider_id": provider_id, "action": "deleted"}


def list_secrets() -> list[dict]:
    """Masks only. The encrypted payload is deliberately not exposed either."""
    out = []
    for provider_id, entry in sorted(_load().items()):
        out.append({
            "provider_id": provider_id,
            "mask": entry.get("mask", "***"),
            "method": entry.get("method", "unknown"),
            "updated_at": entry.get("updated_at"),
        })
    return out


# ── CLI ────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Provider secret store (masks only in output)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--set", metavar="PROVIDER_ID")
    group.add_argument("--delete", metavar="PROVIDER_ID")
    group.add_argument("--has", metavar="PROVIDER_ID")
    args = parser.parse_args()

    if args.list:
        print(json.dumps({"secrets": list_secrets()}, indent=2))
    elif args.set:
        api_key = os.environ.get("SECRET_VALUE", "").strip()
        if not api_key and sys.stdin.isatty():
            api_key = getpass.getpass("API key (input hidden): ").strip()
        result = set_secret(args.set, api_key)
        print(json.dumps(result, indent=2))
    elif args.delete:
        print(json.dumps(delete_secret(args.delete), indent=2))
    elif args.has:
        print(json.dumps({"provider_id": args.has.lower(), "has_secret": has_secret(args.has)}, indent=2))


if __name__ == "__main__":
    main()
