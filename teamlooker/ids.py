"""Device IDs and one-time session passwords."""

from __future__ import annotations

import hashlib
import secrets

# No 0/O, 1/l/I: easy to read aloud over the phone.
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def device_id_from_public_key(public_key: bytes) -> str:
    """Derive a stable 9-digit ID from a host's Ed25519 public key."""
    digest = hashlib.sha256(b"teamlooker-device-id:" + public_key).digest()
    return str(100_000_000 + int.from_bytes(digest[:8], "big") % 900_000_000)


def normalize_id(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def format_id(value: str) -> str:
    digits = normalize_id(value)
    return " ".join(digits[i:i + 3] for i in range(0, len(digits), 3))


def generate_password(length: int = 6) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
