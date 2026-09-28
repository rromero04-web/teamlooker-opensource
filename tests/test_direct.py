import asyncio

import pytest

from teamlooker.ids import (decode_connection_code, encode_connection_code,
                            format_connection_code, parse_target)
from teamlooker.secure import AuthError
from teamlooker.viewer import ViewerSession


def connect(direct_host, password, **kwargs):
    return ViewerSession.direct("127.0.0.1", direct_host.direct_address.port, password,
                                name="tester", **kwargs)


async def wait_for(predicate, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class Inbox:
    def __init__(self):
        self.items = []

    async def __call__(self, msg, data):
        self.items.append((msg, data))

    def of(self, kind):
        return [(m, d) for m, d in self.items if m["type"] == kind]


async def test_direct_session_no_server(direct_host):
    """A full session works peer-to-peer, with no relay involved."""
    assert direct_host.mode == "direct"
    assert direct_host.direct_address.port > 0
    inbox = Inbox()
    viewer = connect(direct_host, direct_host.session_password, on_message=inbox)
    info = await viewer.connect()
    assert info["type"] == "session_info"
    runner = asyncio.create_task(viewer.run())
    await wait_for(lambda: inbox.of("frame"))
    await viewer.send({"type": "chat", "text": "directo!"})
    await wait_for(lambda: any(e["type"] == "chat" for e in direct_host.events))
    await viewer.close()
    await asyncio.wait_for(runner, 5)


async def test_direct_wrong_password(direct_host):
    with pytest.raises(AuthError) as err:
        await connect(direct_host, "incorrecta").connect()
    assert err.value.reason == "password"


async def test_direct_connect_via_code(direct_host):
    """The code shown to the user round-trips back to host:port."""
    code = encode_connection_code("127.0.0.1", direct_host.direct_address.port)
    host, port = parse_target(format_connection_code(code))
    assert (host, port) == ("127.0.0.1", direct_host.direct_address.port)
    viewer = ViewerSession.direct(host, port, direct_host.session_password, name="code")
    await viewer.connect()
    await wait_for(lambda: direct_host.sessions)
    await viewer.close()


def test_connection_code_roundtrip():
    for ip, port in [("192.168.1.50", 7570), ("8.8.8.8", 65535), ("10.0.0.1", 1)]:
        code = encode_connection_code(ip, port)
        assert len(code) == 10
        assert decode_connection_code(code) == (ip, port)
        assert decode_connection_code(format_connection_code(code)) == (ip, port)
    # Confusable characters are tolerated.
    code = encode_connection_code("1.2.3.4", 100)
    assert decode_connection_code(code.replace("O", "0")) == ("1.2.3.4", 100)


def test_parse_target_forms():
    assert parse_target("192.168.0.5:9000") == ("192.168.0.5", 9000)
    assert parse_target("10.0.0.9")[0] == "10.0.0.9"
    assert parse_target("  ") is None
    assert parse_target("host.example.com:1234") == ("host.example.com", 1234)


def test_encode_rejects_bad_input():
    assert encode_connection_code("not-an-ip", 10) is None
    assert decode_connection_code("short") is None
    assert decode_connection_code("!!!!!!!!!!") is None
