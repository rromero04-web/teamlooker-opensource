import socket
import struct

from teamlooker import nat
def test_natpmp_public_ip_parse():
    packet = b"\x00\x80\x00\x00" + b"\x00\x00\x00\x00" + socket.inet_aton("203.0.113.7")
    assert nat._natpmp_parse_public_ip(packet) == "203.0.113.7"
    assert nat._natpmp_parse_public_ip(b"\x00\x81\x00\x01") is None
def test_natpmp_mapping_parse():
    packet = struct.pack("!BBHIHHI", 0, 129, 0, 0, 7570, 40000, 3600)
    internal, external = nat._natpmp_parse_mapping(packet)
    assert internal == 7570 and external == 40000
def test_find_service_from_igd_description():
    xml = b"""<?xml version="1.0"?>
    <root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>
      <service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
      <controlURL>/ctl/IPConn</controlURL></service>
    </serviceList></device></root>"""
    control, stype = nat._find_service(xml)
    assert control == "/ctl/IPConn"
    assert stype.endswith("WANIPConnection:1")
    assert nat._find_service(b"<root/>") is None
def test_local_ips_returns_something():
    ips = nat.local_ips()
    assert all("." in ip for ip in ips)
async def test_map_port_fails_gracefully_without_router():
    # No IGD/NAT-PMP router in the sandbox: must return None, never raise.
    assert await nat.map_port(0) is None
