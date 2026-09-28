"""Application message encoding: a JSON header plus an optional binary body."""

from __future__ import annotations

import json


def pack(msg: dict, data: bytes = b"") -> bytes:
    header = json.dumps(msg, separators=(",", ":")).encode()
    return len(header).to_bytes(4, "big") + header + data


def unpack(buf: bytes) -> tuple[dict, bytes]:
    if len(buf) < 4:
        raise ValueError("message too short")
    size = int.from_bytes(buf[:4], "big")
    if size > len(buf) - 4:
        raise ValueError("truncated message header")
    msg = json.loads(buf[4:4 + size])
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError("message header must be an object with a type")
    return msg, buf[4 + size:]
