import pytest

from teamlooker import ids
from teamlooker.messages import pack, unpack
from teamlooker.srp import SRPClient, SRPServer, create_verifier


def test_srp_success_and_shared_key():
    salt, verifier = create_verifier("s3cret")
    server = SRPServer(salt, verifier)
    client = SRPClient("s3cret")
    m1, key = client.process_challenge(salt, server.B)
    server.process_client(client.A)
    m2 = server.verify(m1)
    assert m2 is not None
    assert client.verify_server(m1, key, m2)
    assert key == server.session_key


def test_srp_wrong_password():
    salt, verifier = create_verifier("s3cret")
    server = SRPServer(salt, verifier)
    client = SRPClient("guess")
    m1, _ = client.process_challenge(salt, server.B)
    server.process_client(client.A)
    assert server.verify(m1) is None


def test_srp_rejects_zero_public_value():
    salt, verifier = create_verifier("x")
    from teamlooker.srp import N, SRPError
    with pytest.raises(SRPError):
        SRPServer(salt, verifier).process_client(N)
    with pytest.raises(SRPError):
        SRPClient("x").process_challenge(salt, 0)


def test_ids():
    device_id = ids.device_id_from_public_key(b"\x01" * 32)
    assert len(device_id) == 9 and device_id.isdigit()
    assert device_id == ids.device_id_from_public_key(b"\x01" * 32)
    assert ids.format_id("123456789") == "123 456 789"
    assert ids.normalize_id(" 123 456-789 ") == "123456789"
    password = ids.generate_password()
    assert len(password) == 6 and set(password) <= set(ids.PASSWORD_ALPHABET)


def test_message_roundtrip():
    msg, data = unpack(pack({"type": "frame", "n": 1}, b"\x00\x01"))
    assert msg == {"type": "frame", "n": 1} and data == b"\x00\x01"
    with pytest.raises(ValueError):
        unpack(b"\x00\x00\x00\x10{}")
