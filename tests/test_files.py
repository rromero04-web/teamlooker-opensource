import hashlib

import pytest

from teamlooker.files import (FileOpError, TransferReceiver, fs_operation, list_dir,
                              safe_name, send_file)
from teamlooker.messages import pack, unpack


def test_safe_name():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\Windows\\evil.exe") == "evil.exe"
    for bad in ("", "..", ".", "/"):
        with pytest.raises(FileOpError):
            safe_name(bad)


def test_list_and_ops(tmp_path):
    (tmp_path / "b.txt").write_text("hi")
    fs_operation("mkdir", {"path": str(tmp_path), "name": "a"})
    listing = list_dir(str(tmp_path))
    assert [e["name"] for e in listing["entries"]] == ["a", "b.txt"]
    assert listing["entries"][0]["dir"] and listing["entries"][1]["size"] == 2
    fs_operation("rename", {"path": str(tmp_path / "b.txt"), "name": "c.txt"})
    fs_operation("delete", {"path": str(tmp_path / "c.txt")})
    (tmp_path / "a" / "x").write_text("1")
    with pytest.raises(FileOpError):
        fs_operation("delete", {"path": str(tmp_path / "a")})  # not empty


async def test_transfer_roundtrip(tmp_path):
    src = tmp_path / "src.bin"
    payload = bytes(range(256)) * 1000
    src.write_bytes(payload)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "src.bin").write_text("existing")
    receiver = TransferReceiver()
    result = {}

    async def deliver(msg, data=b""):
        msg, data = unpack(pack(msg, data))
        if msg["type"] == "file_begin":
            receiver.begin(msg["tid"], dest, msg["name"], msg["size"])
        elif msg["type"] == "file_chunk":
            receiver.chunk(msg["tid"], data)
        elif msg["type"] == "file_end":
            result["path"] = receiver.end(msg["tid"], msg["sha256"])

    await send_file(deliver, "t1", src)
    assert result["path"].name == "src (1).bin"
    assert result["path"].read_bytes() == payload


def test_corrupted_transfer_is_discarded(tmp_path):
    receiver = TransferReceiver()
    receiver.begin("t", tmp_path, "f.txt", 3)
    receiver.chunk("t", b"abc")
    with pytest.raises(FileOpError):
        receiver.end("t", hashlib.sha256(b"xyz").hexdigest())
    assert list(tmp_path.iterdir()) == []
