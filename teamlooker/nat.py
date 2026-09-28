"""Serverless NAT traversal: ask the host's own router to open a port.

Two standard router protocols are tried, in order, and both talk only to the
local gateway — never to any external server:

* **NAT-PMP / PCP** (Apple/RFC 6886, UDP 5351): a tiny binary request.
* **UPnP IGD** (SSDP discovery + SOAP ``AddPortMapping``): common on consumer
  routers.

Everything is best-effort and heavily time-boxed: if the router does not
support it (or it is disabled, or the host is behind carrier-grade NAT) the
functions return ``None`` and the app falls back to a manually forwarded port.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger("teamlooker.nat")

DISCOVERY_TIMEOUT = 3.0
HTTP_TIMEOUT = 4.0
MAPPING_LIFETIME = 3600
DESCRIPTION = "TeamLooker"


@dataclass
class PortMapping:
    external_ip: str
    external_port: int
    internal_port: int
    method: str  # "nat-pmp" or "upnp"
    _cleanup: object = None  # coroutine factory for unmapping

    async def release(self) -> None:
        if self._cleanup is not None:
            try:
                await self._cleanup()
            except Exception:
                pass


def local_ips() -> list[str]:
    """Return this host's non-loopback IPv4 addresses, best guess first."""
    ips: list[str] = []
    # The address used to reach the public internet is the most useful one.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def default_gateways() -> list[str]:
    """Best-effort list of candidate default-gateway addresses."""
    gateways: list[str] = []
    try:  # Linux: parse the kernel routing table.
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    gw = int(fields[2], 16)
                    gateways.append(socket.inet_ntoa(struct.pack("<L", gw)))
    except OSError:
        pass
    # Fallback heuristic: the router is usually x.y.z.1 on the local subnet.
    for ip in local_ips():
        guess = ip.rsplit(".", 1)[0] + ".1"
        if guess not in gateways:
            gateways.append(guess)
    return gateways


# --------------------------------------------------------------------------- NAT-PMP

def _natpmp_parse_public_ip(data: bytes) -> str | None:
    if len(data) >= 12 and data[0] == 0 and data[1] == 128 and data[2:4] == b"\x00\x00":
        return socket.inet_ntoa(data[8:12])
    return None


def _natpmp_parse_mapping(data: bytes) -> tuple[int, int] | None:
    # Response op 129 (=1+128) for TCP: internal port + mapped external port.
    if len(data) >= 16 and data[1] == 129 and data[2:4] == b"\x00\x00":
        internal, external = struct.unpack("!HH", data[8:12])
        return internal, external
    return None


async def _natpmp(gateway: str, port: int) -> PortMapping | None:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        # 1) public address request: version 0, op 0.
        sock.sendto(b"\x00\x00", (gateway, 5351))
        data = await _recv(loop, sock, 1.0)
        public = _natpmp_parse_public_ip(data)
        if not public:
            return None
        # 2) map TCP: version 0, op 2, reserved, internal port, suggested external, lifetime.
        req = struct.pack("!BBHHHI", 0, 2, 0, port, port, MAPPING_LIFETIME)
        sock.sendto(req, (gateway, 5351))
        data = await _recv(loop, sock, 1.0)
        parsed = _natpmp_parse_mapping(data)
        if not parsed:
            return None
        external_port = parsed[1]

        async def cleanup() -> None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            # Lifetime 0 removes the mapping.
            s.sendto(struct.pack("!BBHHHI", 0, 2, 0, port, 0, 0), (gateway, 5351))
            s.close()

        log.info("NAT-PMP mapped %s -> %s:%s via %s", port, public, external_port, gateway)
        return PortMapping(public, external_port, port, "nat-pmp", cleanup)
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        sock.close()


async def _recv(loop, sock, timeout: float) -> bytes:
    return await asyncio.wait_for(loop.sock_recv(sock, 32), timeout)


# ----------------------------------------------------------------------------- UPnP

_SSDP_QUERY = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
)


async def _ssdp_discover() -> list[str]:
    """Return device-description URLs advertised by IGD routers."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    locations: list[str] = []
    try:
        sock.sendto(_SSDP_QUERY.encode(), ("239.255.255.250", 1900))
        deadline = loop.time() + DISCOVERY_TIMEOUT
        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 2048),
                                              deadline - loop.time())
            except asyncio.TimeoutError:
                break
            match = re.search(rb"LOCATION:\s*(\S+)", data, re.IGNORECASE)
            if match:
                loc = match.group(1).decode(errors="ignore").strip()
                if loc not in locations:
                    locations.append(loc)
    except OSError:
        pass
    finally:
        sock.close()
    return locations


async def _http(method: str, url: str, body: str = "", headers: dict | None = None) -> bytes | None:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parts.hostname, port), HTTP_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    request = [f"{method} {path} HTTP/1.1", f"Host: {parts.hostname}:{port}",
               "Connection: close", f"Content-Length: {len(body.encode())}"]
    for key, value in (headers or {}).items():
        request.append(f"{key}: {value}")
    request.append("")
    request.append(body)
    try:
        writer.write("\r\n".join(request).encode())
        await writer.drain()
        # The server sets Connection: close, so read until EOF (may span segments).
        raw = await asyncio.wait_for(reader.read(), HTTP_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        raw = None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    if not raw or b"\r\n\r\n" not in raw:
        return None
    return raw.split(b"\r\n\r\n", 1)[1]


def _find_service(desc_xml: bytes) -> tuple[str, str] | None:
    """Return (control_url_path, service_type) for a WAN connection service."""
    try:
        root = ET.fromstring(desc_xml)
    except ET.ParseError:
        return None
    ns = "{urn:schemas-upnp-org:device-1-0}"
    wanted = ("urn:schemas-upnp-org:service:WANIPConnection:1",
              "urn:schemas-upnp-org:service:WANPPPConnection:1",
              "urn:schemas-upnp-org:service:WANIPConnection:2")
    for service in root.iter(f"{ns}service"):
        stype = service.findtext(f"{ns}serviceType", "")
        control = service.findtext(f"{ns}controlURL", "")
        if stype in wanted and control:
            return control, stype
    return None


def _soap(action: str, service_type: str, args: list[tuple[str, str]]) -> tuple[str, str]:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service_type}">{body}</u:{action}>'
        "</s:Body></s:Envelope>")
    return envelope, f'"{service_type}#{action}"'


async def _upnp(port: int) -> PortMapping | None:
    for location in await _ssdp_discover():
        desc = await _http("GET", location)
        if not desc:
            continue
        found = _find_service(desc)
        if not found:
            continue
        control_path, stype = found
        control_url = urllib.parse.urljoin(location, control_path)
        internal_ip = local_ips()[0] if local_ips() else None
        if not internal_ip:
            return None
        body, action_header = _soap("AddPortMapping", stype, [
            ("NewRemoteHost", ""), ("NewExternalPort", str(port)),
            ("NewProtocol", "TCP"), ("NewInternalPort", str(port)),
            ("NewInternalClient", internal_ip), ("NewEnabled", "1"),
            ("NewPortMappingDescription", DESCRIPTION),
            ("NewLeaseDuration", str(MAPPING_LIFETIME))])
        result = await _http("POST", control_url, body,
                             {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": action_header})
        if result is None or b"AddPortMappingResponse" not in result and b"Envelope" not in result:
            continue
        if result and b"errorCode" in result:
            continue
        ext_body, ext_header = _soap("GetExternalIPAddress", stype, [])
        ext = await _http("POST", control_url, ext_body,
                          {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": ext_header})
        external_ip = internal_ip
        if ext:
            m = re.search(rb"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", ext)
            if m and m.group(1):
                external_ip = m.group(1).decode()

        async def cleanup() -> None:
            del_body, del_header = _soap("DeletePortMapping", stype, [
                ("NewRemoteHost", ""), ("NewExternalPort", str(port)), ("NewProtocol", "TCP")])
            await _http("POST", control_url, del_body,
                        {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": del_header})

        log.info("UPnP mapped %s -> %s:%s", port, external_ip, port)
        return PortMapping(external_ip, port, port, "upnp", cleanup)
    return None


async def map_port(port: int) -> PortMapping | None:
    """Try to open ``port`` on the local router. Returns ``None`` on failure."""
    for gateway in default_gateways():
        try:
            mapping = await asyncio.wait_for(_natpmp(gateway, port), 2.5)
        except asyncio.TimeoutError:
            mapping = None
        if mapping:
            return mapping
    try:
        return await asyncio.wait_for(_upnp(port), DISCOVERY_TIMEOUT + HTTP_TIMEOUT * 3)
    except asyncio.TimeoutError:
        return None
