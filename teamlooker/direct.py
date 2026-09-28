"""Direct peer-to-peer transport (no server in the middle).

The host listens on a TCP port; the viewer connects straight to the host's
address. The same SRP + AES-256-GCM handshake used for the relay runs over this
socket, so authentication and end-to-end encryption are identical — the only
difference is that the bytes travel directly between the two PCs.

On the same LAN this needs no configuration. Across different networks the host
tries to open its port automatically with UPnP/NAT-PMP (:mod:`teamlooker.nat`);
if that fails the user forwards the port manually.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from teamlooker import DEFAULT_DIRECT_PORT
from teamlooker.framing import close_writer
from teamlooker.ids import encode_connection_code
from teamlooker.nat import PortMapping, local_ips, map_port

log = logging.getLogger("teamlooker.direct")

ConnectionHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, str], Awaitable[None]]


@dataclass
class DirectAddress:
    """Everything a viewer might need to reach this host."""
    port: int
    local_ips: list[str] = field(default_factory=list)
    public_ip: str | None = None
    public_port: int | None = None
    mapping_method: str | None = None

    @property
    def code(self) -> str | None:
        if self.public_ip:
            return encode_connection_code(self.public_ip, self.public_port or self.port)
        if self.local_ips:
            return encode_connection_code(self.local_ips[0], self.port)
        return None

    def as_dict(self) -> dict:
        return {
            "port": self.port,
            "local_ips": self.local_ips,
            "public_ip": self.public_ip,
            "public_port": self.public_port,
            "mapping_method": self.mapping_method,
            "code": self.code,
        }


class DirectHost:
    """A TCP listener that hands each incoming connection to a handler."""

    def __init__(self, handler: ConnectionHandler, port: int = DEFAULT_DIRECT_PORT,
                 bind: str = "0.0.0.0", enable_upnp: bool = True):
        self._handler = handler
        self.port = port
        self.bind = bind
        self.enable_upnp = enable_upnp
        self._server: asyncio.base_events.Server | None = None
        self._mapping: PortMapping | None = None
        self.address = DirectAddress(port=port)

    async def start(self) -> DirectAddress:
        self._server = await asyncio.start_server(self._on_client, self.bind, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self.address = DirectAddress(port=self.port, local_ips=local_ips())
        log.info("direct host listening on %s:%s", self.bind, self.port)
        if self.enable_upnp:
            asyncio.create_task(self._map())
        return self.address

    async def _map(self) -> None:
        try:
            mapping = await map_port(self.port)
        except Exception as exc:  # best effort only
            log.debug("port mapping failed: %s", exc)
            mapping = None
        if mapping:
            self._mapping = mapping
            self.address.public_ip = mapping.external_ip
            self.address.public_port = mapping.external_port
            self.address.mapping_method = mapping.method

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        try:
            await self._handler(reader, writer, ip)
        except Exception:
            log.exception("direct connection handler failed")
            await close_writer(writer)

    async def stop(self) -> None:
        if self._mapping is not None:
            await self._mapping.release()
            self._mapping = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None


async def open_direct(host: str, port: int, timeout: float = 12.0):
    """Viewer side: open a raw connection to a host address."""
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
