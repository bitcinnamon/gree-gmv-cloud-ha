"""Captured control-envelope implementation for Gree GMV DTU cloud control."""

from __future__ import annotations

import base64
from functools import lru_cache
import json
import secrets
import string
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAg1kX4Uj8dzmIq8c2r37t
uDfEwldT/+f9drcuQHtXW5ne7Q3XMh++Ox0cefSLpkRCkU+PbRlQzZBqqgyEN9l
T53OlHx1Coqj6+6wv2IWm3Ebp5p1j0Tgknf7VJW7A5/avhGOlKzQ/05Pi/kx7s
Z/kWcmdmc8eVZkjQClU6gBVrssHjZbc86uI8R7x4le7WWzRMaYvXVPpZqUcHgK+
k+nNt38sXL7Nn1a1KTWLltV0tOxMyQ/bL88n+PRQX3c9oLgLDd7Xb4HDWWfZgi
qS/33bfoSxCMXEiCuFcZv7ToegHCS1+JjE1pTKoZ46NWC7rmHm8y66om/xR1iv
A2qzIjQUAwIDAQAB
-----END PUBLIC KEY-----"""

AES_IV = b"****************"
SESSION_KEY_ALPHABET = string.ascii_letters + string.digits
CONTROL_FIELDS = (
    "openId",
    "mac",
    "ip",
    "setTemp",
    "on_OFF_Status",
    "mode",
    "windSpeed",
    "systemId",
    "bindType",
    "timestamp",
)


def generate_session_key() -> str:
    """Generate the mini-program's 16-character alphanumeric AES key."""
    return "".join(secrets.choice(SESSION_KEY_ALPHABET) for _ in range(16))


def normalize_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete DTU control object and preserve field order."""
    missing = [field for field in CONTROL_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"Missing control fields: {', '.join(missing)}")
    normalized = {field: payload[field] for field in CONTROL_FIELDS}
    if normalized["bindType"] != "DTU":
        raise ValueError("bindType must be DTU")
    if normalized["on_OFF_Status"] not in (0, 1):
        raise ValueError("on_OFF_Status must be 0 or 1")
    if normalized["mode"] not in (1, 2, 3, 4, 5):
        raise ValueError("mode must be 1 through 5")
    if normalized["windSpeed"] not in (1, 2, 3, 4, 5, 6):
        raise ValueError("windSpeed must be 1 through 6")
    temperature = float(normalized["setTemp"])
    if temperature < 16 or temperature > 30 or not (temperature * 2).is_integer():
        raise ValueError("setTemp must use 0.5-degree steps from 16 to 30")
    if not isinstance(normalized["timestamp"], int) or normalized["timestamp"] <= 0:
        raise ValueError("timestamp must be a positive millisecond value")
    return normalized


@lru_cache(maxsize=1)
def _public_key():
    return serialization.load_pem_public_key(PUBLIC_KEY_PEM)


def encrypt_control_payload(
    payload: dict[str, Any], *, session_key: str | None = None
) -> dict[str, str]:
    """Create the AES-CBC plus RSA PKCS#1 v1.5 request envelope."""
    normalized = normalize_control_payload(payload)
    key_text = session_key or generate_session_key()
    if len(key_text) != 16 or any(char not in SESSION_KEY_ALPHABET for char in key_text):
        raise ValueError("session_key must be 16 ASCII alphanumeric characters")
    key = key_text.encode("ascii")
    plaintext = json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":")
    ).encode()
    remainder = len(plaintext) % 16
    if remainder:
        plaintext += bytes(16 - remainder)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(AES_IV)).encryptor()
    request_data = encryptor.update(plaintext) + encryptor.finalize()
    encrypted_key = _public_key().encrypt(key, padding.PKCS1v15())
    return {
        "requestData": base64.b64encode(request_data).decode("ascii"),
        "encrypted": base64.b64encode(encrypted_key).decode("ascii"),
    }
