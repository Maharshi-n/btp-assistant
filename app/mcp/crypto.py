"""Encrypt/decrypt MCP server env vars using the existing Fernet key."""
from __future__ import annotations

import json

import app.config as app_config


def _fernet():
    from cryptography.fernet import Fernet
    key = app_config.FERNET_KEY
    if not key:
        raise RuntimeError("FERNET_KEY not set — cannot encrypt MCP env vars")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_env(env: dict) -> str:
    """Serialize and encrypt a dict of env vars. Returns a str token."""
    return _fernet().encrypt(json.dumps(env).encode()).decode()


def decrypt_env(token: str) -> dict:
    """Decrypt and deserialize env vars. Returns a dict."""
    if not token:
        return {}
    return json.loads(_fernet().decrypt(token.encode()))
