"""Relay / broker server.

Hosts keep a control connection open and prove ownership of their ID with an
Ed25519 signature. A viewer asks for a target ID; the relay tells the host,
the host opens a second connection for that session, and from then on the
relay blindly pipes bytes between the two sockets. Everything after pairing is
end-to-end encrypted by the peers, so the relay never sees screen contents,
keystrokes, files or passwords.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import secrets
import ssl
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from teamlooker import DEFAULT_RELAY_PORT
from teamlooker.framing import FrameError, close_writer, recv_json, send_json
from teamlooker.ids import device_id_from_public_key, normalize_id

log = logging.getLogger("teamlooker.relay")

PAIR_TIMEOUT = 15
HOST_IDLE_TIMEOUT = 90
PING_INTERVAL = 30
FIRST_FRAME_TIMEOUT = 15
PIPE_CHUNK = 64 * 1024


@dataclass
class HostEntry:
    device_id: str
    writer: asyncio.StreamWriter
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, obj: dict) -> None:
        async with self.lock:
            await send_json(self.writer, obj)


@dataclass
class PendingSession:
    target: str
    viewer_writer: asyncio.StreamWriter
    viewer_reader: asyncio.StreamReader
    paired: asyncio.Future


class RateLimiter:
    def __init__(self, max_events: int, window: float):
        self.max_events = max_events
        self.window = window
        self._events: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] > self.window:
            events.popleft()
        if len(events) >= self.max_events:
            return False
        events.append(now)
        return True


class RelayServer:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_RELAY_PORT,
                 ssl_context: ssl.SSLContext | None = None,
                 connects_per_minute: int = 30):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.hosts: dict[str, HostEntry] = {}
        self.pending: dict[str, PendingSession] = {}
        self.limiter = RateLimiter(connects_per_minute, 60)
        self.active_pipes = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port,
                                                  ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("relay listening on %s:%s%s", self.host, self.port,
                 " (TLS)" if self.ssl_context else "")

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for entry in list(self.hosts.values()):
                await close_writer(entry.writer)
            for pending in list(self.pending.values()):
                await close_writer(pending.viewer_writer)
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        keep_open = False
        try:
            first = await asyncio.wait_for(recv_json(reader), FIRST_FRAME_TIMEOUT)
            kind = first.get("type")
            if kind == "register":
                await self._handle_host(reader, writer, first)
            elif kind == "connect":
                keep_open = await self._handle_viewer(reader, writer, first, ip)
            elif kind == "accept":
                keep_open = await self._handle_accept(reader, writer, first)
            elif kind == "status":
                await send_json(writer, {"type": "status", "hosts": len(self.hosts),
                                         "sessions": self.active_pipes})
            else:
                await send_json(writer, {"type": "error", "code": "bad_request"})
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                FrameError, ssl.SSLError):
            pass
        except Exception:
            log.exception("error handling connection from %s", ip)
        finally:
            if not keep_open:
                await close_writer(writer)

    # --- hosts -----------------------------------------------------------------

    async def _handle_host(self, reader, writer, first: dict) -> None:
        try:
            public_raw = base64.b64decode(first["pubkey"])
            public_key = Ed25519PublicKey.from_public_bytes(public_raw)
        except Exception:
            await send_json(writer, {"type": "error", "code": "bad_key"})
            return
        nonce = secrets.token_bytes(32)
        await send_json(writer, {"type": "challenge", "nonce": base64.b64encode(nonce).decode()})
        proof = await asyncio.wait_for(recv_json(reader), FIRST_FRAME_TIMEOUT)
        try:
            public_key.verify(base64.b64decode(proof["sig"]), b"teamlooker-register:" + nonce)
        except (InvalidSignature, KeyError, ValueError, TypeError):
            await send_json(writer, {"type": "error", "code": "bad_signature"})
            return
        device_id = device_id_from_public_key(public_raw)
        old = self.hosts.get(device_id)
        entry = HostEntry(device_id, writer)
        self.hosts[device_id] = entry
        if old is not None:
            # The same device reconnected (e.g. network change): newest wins.
            await close_writer(old.writer)
        log.info("host %s online", device_id)
        await entry.send({"type": "registered", "id": device_id})
        pinger = asyncio.create_task(self._ping_loop(entry))
        try:
            while True:
                msg = await asyncio.wait_for(recv_json(reader), HOST_IDLE_TIMEOUT)
                if msg.get("type") == "reject":
                    pending = self.pending.get(str(msg.get("session")))
                    if pending and pending.target == device_id and not pending.paired.done():
                        pending.paired.set_result(None)
        finally:
            pinger.cancel()
            if self.hosts.get(device_id) is entry:
                del self.hosts[device_id]
                log.info("host %s offline", device_id)

    async def _ping_loop(self, entry: HostEntry) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await entry.send({"type": "ping"})
        except (ConnectionError, RuntimeError):
            pass

    # --- viewers ---------------------------------------------------------------

    async def _handle_viewer(self, reader, writer, first: dict, ip: str) -> bool:
        target = normalize_id(first.get("target", ""))
        if not self.limiter.allow(ip):
            await send_json(writer, {"type": "error", "code": "rate_limited"})
            return False
        host = self.hosts.get(target)
        if host is None:
            await send_json(writer, {"type": "error", "code": "offline"})
            return False
        session_id = secrets.token_urlsafe(24)
        paired = asyncio.get_running_loop().create_future()
        self.pending[session_id] = PendingSession(target, writer, reader, paired)
        try:
            await host.send({"type": "incoming", "session": session_id, "peer": ip})
            result = await asyncio.wait_for(paired, PAIR_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError):
            result = None
        finally:
            self.pending.pop(session_id, None)
        if result is None:
            await send_json(writer, {"type": "error", "code": "host_unreachable"})
            return False
        # The accepting side owns both sockets from here on.
        return True

    async def _handle_accept(self, host_reader, host_writer, first: dict) -> bool:
        pending = self.pending.get(str(first.get("session")))
        if pending is None or pending.paired.done():
            await send_json(host_writer, {"type": "error", "code": "unknown_session"})
            return False
        pending.paired.set_result(True)
        await send_json(pending.viewer_writer, {"type": "paired"})
        await send_json(host_writer, {"type": "paired"})
        self.active_pipes += 1
        try:
            await asyncio.gather(
                _pipe(pending.viewer_reader, host_writer),
                _pipe(host_reader, pending.viewer_writer),
            )
        finally:
            self.active_pipes -= 1
            await close_writer(pending.viewer_writer)
            await close_writer(host_writer)
        return True


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(PIPE_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        # Closing one direction tears the whole session down.
        await close_writer(writer)


def build_ssl_context(certfile: str | None, keyfile: str | None) -> ssl.SSLContext | None:
    if not certfile:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT)
    parser.add_argument("--certfile", help="TLS certificate (optional)")
    parser.add_argument("--keyfile", help="TLS private key (optional)")
    parser.add_argument("--connects-per-minute", type=int, default=30,
                        help="max connection attempts per viewer IP per minute")


async def run_from_args(args: argparse.Namespace) -> None:
    server = RelayServer(args.bind, args.port, build_ssl_context(args.certfile, args.keyfile),
                         args.connects_per_minute)
    await server.serve_forever()
