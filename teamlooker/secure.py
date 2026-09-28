"""End-to-end encrypted channel between a viewer and a host.

The relay only ever sees the SRP handshake (safe to observe) and AES-256-GCM
ciphertext. Each direction has its own key and a strictly increasing nonce
counter, so replayed, reordered or tampered frames fail to decrypt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from teamlooker import PROTOCOL_VERSION
from teamlooker.framing import close_writer, read_frame, recv_json, send_json, write_frame
from teamlooker.messages import pack, unpack
from teamlooker.srp import SRPClient, SRPError, SRPServer, hex_to_int, int_to_hex

HANDSHAKE_TIMEOUT = 20


class AuthError(Exception):
    def __init__(self, reason: str, retry_after: float = 0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class ChannelClosed(Exception):
    pass


def _derive(key: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"teamlooker/1 " + label).derive(key)


class SecureChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 send_key: bytes, recv_key: bytes):
        self._reader = reader
        self._writer = writer
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self.closed = False

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return counter.to_bytes(12, "big")

    async def send(self, payload: bytes) -> None:
        if self.closed:
            raise ChannelClosed()
        frame = self._send.encrypt(self._nonce(self._send_ctr), payload, None)
        self._send_ctr += 1
        try:
            write_frame(self._writer, frame)
            await self._writer.drain()
        except (ConnectionError, RuntimeError) as exc:
            self.closed = True
            raise ChannelClosed() from exc

    async def recv(self) -> bytes:
        try:
            frame = await read_frame(self._reader)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            self.closed = True
            raise ChannelClosed() from exc
        try:
            payload = self._recv.decrypt(self._nonce(self._recv_ctr), frame, None)
        except Exception as exc:
            self.closed = True
            raise ChannelClosed("decryption failed") from exc
        self._recv_ctr += 1
        return payload

    async def send_msg(self, msg: dict, data: bytes = b"") -> None:
        await self.send(pack(msg, data))

    async def recv_msg(self) -> tuple[dict, bytes]:
        return unpack(await self.recv())

    async def close(self) -> None:
        self.closed = True
        await close_writer(self._writer)


@dataclass
class Verifier:
    salt: bytes
    verifier: bytes
    label: str


async def _fail(writer, reason: str, **extra) -> AuthError:
    await send_json(writer, {"type": "auth_fail", "reason": reason, **extra})
    return AuthError(reason, extra.get("retry_after", 0))


async def host_handshake(reader, writer, verifiers: list[Verifier],
                         locked_for: float = 0) -> tuple[SecureChannel, str]:
    """Authenticate an incoming viewer.

    Returns ``(channel, label_of_matching_password)``. Raises :class:`AuthError`
    (after telling the viewer why) when authentication fails; ``reason`` is
    ``"password"`` for a wrong password.
    """
    hello = await asyncio.wait_for(recv_json(reader), HANDSHAKE_TIMEOUT)
    if hello.get("type") != "hello" or hello.get("proto") != PROTOCOL_VERSION:
        raise await _fail(writer, "protocol")
    if locked_for > 0:
        raise await _fail(writer, "locked", retry_after=max(1, round(locked_for)))
    if not verifiers:
        raise await _fail(writer, "no_password")
    try:
        a_pub = hex_to_int(hello["A"])
        servers = [SRPServer(v.salt, v.verifier) for v in verifiers]
        for server in servers:
            server.process_client(a_pub)
    except (KeyError, ValueError, TypeError, SRPError):
        raise await _fail(writer, "protocol")
    await send_json(writer, {"type": "challenge", "offers": [
        {"salt": s.salt.hex(), "B": int_to_hex(s.B)} for s in servers]})
    proof = await asyncio.wait_for(recv_json(reader), HANDSHAKE_TIMEOUT)
    proofs = proof.get("M1") if proof.get("type") == "proof" else None
    if not isinstance(proofs, list) or len(proofs) != len(servers):
        raise await _fail(writer, "protocol")
    for index, (server, m1_hex) in enumerate(zip(servers, proofs)):
        try:
            m2 = server.verify(bytes.fromhex(m1_hex))
        except (ValueError, TypeError):
            m2 = None
        if m2 is not None:
            await send_json(writer, {"type": "auth_ok", "which": index, "M2": m2.hex()})
            key = server.session_key
            channel = SecureChannel(reader, writer, _derive(key, b"host->viewer"),
                                    _derive(key, b"viewer->host"))
            return channel, verifiers[index].label
    raise await _fail(writer, "password")


async def viewer_handshake(reader, writer, password: str) -> SecureChannel:
    client = SRPClient(password)
    await send_json(writer, {"type": "hello", "proto": PROTOCOL_VERSION,
                             "A": int_to_hex(client.A)})
    msg = await asyncio.wait_for(recv_json(reader), HANDSHAKE_TIMEOUT)
    if msg.get("type") == "auth_fail":
        raise AuthError(msg.get("reason", "unknown"), msg.get("retry_after", 0))
    if msg.get("type") != "challenge" or not isinstance(msg.get("offers"), list):
        raise AuthError("protocol")
    results = []
    try:
        for offer in msg["offers"]:
            results.append(client.process_challenge(bytes.fromhex(offer["salt"]),
                                                    hex_to_int(offer["B"])))
    except (KeyError, ValueError, TypeError, SRPError) as exc:
        raise AuthError("protocol") from exc
    await send_json(writer, {"type": "proof", "M1": [m1.hex() for m1, _ in results]})
    msg = await asyncio.wait_for(recv_json(reader), HANDSHAKE_TIMEOUT)
    if msg.get("type") == "auth_fail":
        raise AuthError(msg.get("reason", "unknown"), msg.get("retry_after", 0))
    try:
        m1, key = results[int(msg["which"])]
        m2 = bytes.fromhex(msg["M2"])
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise AuthError("protocol") from exc
    if msg.get("type") != "auth_ok" or not client.verify_server(m1, key, m2):
        # The peer did not prove it knows the password: it is not the real host.
        raise AuthError("host_verification")
    return SecureChannel(reader, writer, _derive(key, b"viewer->host"),
                         _derive(key, b"host->viewer"))
