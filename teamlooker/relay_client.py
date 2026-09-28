"""Client side of the relay protocol (used by both hosts and viewers)."""

from __future__ import annotations

import asyncio
import base64
import ssl
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from teamlooker import DEFAULT_RELAY_PORT
from teamlooker.framing import close_writer, recv_json, send_json

CONNECT_TIMEOUT = 10

ERROR_MESSAGES = {
    "offline": "El equipo remoto no está conectado al servidor.",
    "rate_limited": "Demasiados intentos de conexión. Espera un minuto.",
    "host_unreachable": "El equipo remoto no respondió a tiempo.",
}


class RelayError(Exception):
    def __init__(self, code: str):
        super().__init__(ERROR_MESSAGES.get(code, code))
        self.code = code


@dataclass
class RelayAddress:
    host: str
    port: int
    tls: bool = False

    @classmethod
    def parse(cls, value: str) -> "RelayAddress":
        value = value.strip()
        tls = False
        if value.startswith("tls://"):
            tls, value = True, value[len("tls://"):]
        elif value.startswith("tcp://"):
            value = value[len("tcp://"):]
        if value.startswith("["):  # [ipv6]:port
            host, _, rest = value[1:].partition("]")
            port = int(rest.lstrip(":") or DEFAULT_RELAY_PORT)
        elif value.count(":") == 1:
            host, port_s = value.split(":")
            port = int(port_s)
        else:
            host, port = value, DEFAULT_RELAY_PORT
        if not host:
            raise ValueError("relay host is empty")
        return cls(host, port, tls)

    def __str__(self) -> str:
        return f"{'tls://' if self.tls else ''}{self.host}:{self.port}"

    async def open(self):
        # With TLS the relay certificate is verified against the system CAs
        # (point SSL_CERT_FILE at your own CA for a self-signed relay).
        ssl_ctx = ssl.create_default_context() if self.tls else None
        return await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_ctx), CONNECT_TIMEOUT)


def public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


async def register_host(relay: RelayAddress, key: Ed25519PrivateKey):
    """Open the host's control connection. Returns ``(reader, writer, device_id)``."""
    reader, writer = await relay.open()
    try:
        await send_json(writer, {"type": "register",
                                 "pubkey": base64.b64encode(public_key_bytes(key)).decode()})
        challenge = await asyncio.wait_for(recv_json(reader), CONNECT_TIMEOUT)
        if challenge.get("type") != "challenge":
            raise RelayError(challenge.get("code", "protocol"))
        nonce = base64.b64decode(challenge["nonce"])
        signature = key.sign(b"teamlooker-register:" + nonce)
        await send_json(writer, {"type": "proof", "sig": base64.b64encode(signature).decode()})
        reply = await asyncio.wait_for(recv_json(reader), CONNECT_TIMEOUT)
        if reply.get("type") != "registered":
            raise RelayError(reply.get("code", "protocol"))
        return reader, writer, reply["id"]
    except BaseException:
        await close_writer(writer)
        raise


async def accept_session(relay: RelayAddress, session_id: str):
    """Host side: open the data connection for an incoming session."""
    reader, writer = await relay.open()
    try:
        await send_json(writer, {"type": "accept", "session": session_id})
        reply = await asyncio.wait_for(recv_json(reader), CONNECT_TIMEOUT)
        if reply.get("type") != "paired":
            raise RelayError(reply.get("code", "protocol"))
        return reader, writer
    except BaseException:
        await close_writer(writer)
        raise


async def connect_to_host(relay: RelayAddress, target_id: str):
    """Viewer side: ask the relay to pair us with ``target_id``."""
    reader, writer = await relay.open()
    try:
        await send_json(writer, {"type": "connect", "target": target_id})
        reply = await asyncio.wait_for(recv_json(reader), CONNECT_TIMEOUT + 20)
        if reply.get("type") != "paired":
            raise RelayError(reply.get("code", "protocol"))
        return reader, writer
    except BaseException:
        await close_writer(writer)
        raise
