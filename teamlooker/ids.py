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


# --- direct-connection codes -----------------------------------------------------
# A connection code packs an IPv4 address + port into a short, readable string so
# a viewer can type it like a TeamViewer ID. Crockford base32 (no I/L/O/U) keeps
# it unambiguous when read aloud.
import socket as _socket

_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_DECODE = {c: i for i, c in enumerate(_CODE_ALPHABET)}


def encode_connection_code(ip: str, port: int) -> str | None:
    """Pack an IPv4 ``ip`` and ``port`` into a 10-character code, or ``None``."""
    try:
        packed = _socket.inet_aton(ip) + int(port).to_bytes(2, "big")
    except (OSError, OverflowError, ValueError):
        return None
    value = int.from_bytes(packed, "big")
    chars = []
    for _ in range(10):
        value, rem = divmod(value, 32)
        chars.append(_CODE_ALPHABET[rem])
    return "".join(reversed(chars))


def format_connection_code(code: str) -> str:
    code = code.upper().replace(" ", "").replace("-", "")
    return "-".join(code[i:i + 5] for i in range(0, len(code), 5))


def decode_connection_code(code: str) -> tuple[str, int] | None:
    """Turn a 10-character code back into ``(ip, port)``."""
    cleaned = "".join(ch for ch in code.upper() if ch not in " -")
    cleaned = cleaned.replace("I", "1").replace("L", "1").replace("O", "0")
    if len(cleaned) != 10 or any(ch not in _CODE_DECODE for ch in cleaned):
        return None
    value = 0
    for ch in cleaned:
        value = value * 32 + _CODE_DECODE[ch]
    packed = value.to_bytes(6, "big")
    return _socket.inet_ntoa(packed[:4]), int.from_bytes(packed[4:], "big")


def parse_target(text: str) -> tuple[str, int] | None:
    """Accept a connection code, ``host:port``, or bare ``host`` (default port)."""
    from teamlooker import DEFAULT_DIRECT_PORT
    text = text.strip()
    if not text:
        return None
    decoded = decode_connection_code(text)
    if decoded:
        return decoded
    host, sep, port = text.rpartition(":")
    if sep and host and port.isdigit():
        return host.strip("[]"), int(port)
    return text.strip("[]"), DEFAULT_DIRECT_PORT
