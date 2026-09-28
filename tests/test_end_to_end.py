import asyncio

import pytest

from teamlooker.relay_client import RelayAddress, RelayError
from teamlooker.screen import apply_frame
from teamlooker.secure import AuthError
from teamlooker.viewer import ViewerSession
from tests.conftest import FakeClipboard


async def wait_for(predicate, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_viewer(relay, host, password, **kwargs):
    return ViewerSession.relay(RelayAddress("127.0.0.1", relay.port), host.device_id, password,
                               name="tester", **kwargs)


class Inbox:
    def __init__(self):
        self.items = []

    async def __call__(self, msg, data):
        self.items.append((msg, data))

    def of(self, kind):
        return [(m, d) for m, d in self.items if m["type"] == kind]


async def test_full_session(relay, host, tmp_path):
    inbox = Inbox()
    clip = FakeClipboard()
    viewer = make_viewer(relay, host, host.session_password, on_message=inbox,
                         clipboard=clip, download_dir=tmp_path / "dl")
    info = await viewer.connect()
    assert info["device_id"] == host.device_id
    assert len(info["monitors"]) == 2
    runner = asyncio.create_task(viewer.run())

    # Screen: full first frame, decodable.
    await wait_for(lambda: inbox.of("frame"))
    meta, blob = inbox.of("frame")[0]
    img = apply_frame(None, meta, blob)
    assert img.size == (320, 200)
    assert meta["cursor"] == [0.5, 0.5]
    await wait_for(lambda: host.sessions)
    session = next(iter(host.sessions.values()))
    assert session.viewer_name == "tester"

    # Flow control: without acks the host stops after MAX_CREDITS frames.
    from tests.conftest import FakeCapturer
    for _ in range(10):
        FakeCapturer.frame_no += 1
        await asyncio.sleep(0.06)
    assert len(inbox.of("frame")) <= 3
    before = len(inbox.of("frame"))
    await viewer.send({"type": "frame_ack"})
    await wait_for(lambda: len(inbox.of("frame")) > before)

    # Input goes to the right place on the selected monitor.
    await viewer.send({"type": "mouse_move", "x": 0.5, "y": 0.5})
    await viewer.send({"type": "monitor", "index": 1})
    await viewer.send({"type": "mouse_button", "button": 2, "down": True, "x": 1, "y": 1})
    await viewer.send({"type": "key", "key": "a", "code": "KeyA", "down": True})
    await viewer.send({"type": "combo", "keys": ["ctrl", "alt", "delete"]})
    fake_input = host.input
    await wait_for(lambda: ("combo", ("ctrl", "alt", "delete")) in fake_input.events)
    assert ("move", 160, 100) in fake_input.events
    assert ("move", 479, 99) in fake_input.events
    assert ("button", 2, True) in fake_input.events
    assert ("key", "a", "KeyA", True) in fake_input.events

    # Chat in both directions.
    await viewer.send({"type": "chat", "text": "hola"})
    await wait_for(lambda: any(e["type"] == "chat" for e in host.events))
    await host.send_chat(session.id, "qué tal")
    await wait_for(lambda: inbox.of("chat"))
    assert inbox.of("chat")[0][0]["text"] == "qué tal"

    # Clipboard sync host -> viewer and viewer -> host.
    host.clipboard.text = "from host"
    await wait_for(lambda: clip.text == "from host", timeout=4)
    clip.text = "from viewer"
    await wait_for(lambda: host.clipboard.text == "from viewer", timeout=4)

    # Remote file manager.
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    (remote_dir / "report.txt").write_text("remote data")
    result = await viewer.request({"type": "fs_list", "path": str(remote_dir)})
    assert result["ok"] and [e["name"] for e in result["data"]["entries"]] == ["report.txt"]
    result = await viewer.request({"type": "fs_op", "op": "mkdir",
                                   "args": {"path": str(remote_dir), "name": "new"}})
    assert result["ok"] and (remote_dir / "new").is_dir()

    # Upload.
    local = tmp_path / "upload.bin"
    local.write_bytes(b"x" * 200_000)
    tid = viewer.upload(str(local), str(remote_dir))
    await wait_for(lambda: any(m.get("tid") == tid for m, _ in inbox.of("transfer_done")))
    done = [m for m, _ in inbox.of("transfer_done") if m["tid"] == tid][0]
    assert done["ok"], done
    assert (remote_dir / "upload.bin").read_bytes() == b"x" * 200_000

    # Download.
    tid = await viewer.download(str(remote_dir / "report.txt"))
    await wait_for(lambda: any(m.get("tid") == tid for m, _ in inbox.of("transfer_done")))
    done = [m for m, _ in inbox.of("transfer_done") if m["tid"] == tid][0]
    assert done["ok"], done
    assert (tmp_path / "dl" / "report.txt").read_text() == "remote data"

    # Permissions are enforced by the host.
    host.config.set_permissions(files=False, control=False)
    result = await viewer.request({"type": "fs_list", "path": str(remote_dir)})
    assert not result["ok"]
    count = len(fake_input.events)
    await viewer.send({"type": "mouse_move", "x": 0.1, "y": 0.1})
    await asyncio.sleep(0.2)
    assert len(fake_input.events) == count

    # Host ends the session.
    await host.disconnect(session.id)
    await asyncio.wait_for(runner, 5)
    assert inbox.of("bye")
    await wait_for(lambda: ("release_all",) in fake_input.events)
    await wait_for(lambda: not host.sessions)


async def test_wrong_password_and_lockout(relay, host):
    for attempt in range(3):
        with pytest.raises(AuthError) as err:
            await make_viewer(relay, host, "wrong").connect()
        assert err.value.reason == "password"
    with pytest.raises(AuthError) as err:
        await make_viewer(relay, host, host.session_password).connect()
    assert err.value.reason == "locked" and err.value.retry_after > 0


async def test_permanent_password(relay, host):
    host.config.set_permanent_password("unattended-pass")
    viewer = make_viewer(relay, host, "unattended-pass")
    await viewer.connect()
    await wait_for(lambda: host.sessions)
    assert next(iter(host.sessions.values())).auth_label == "permanent"
    await viewer.close()
    await wait_for(lambda: not host.sessions)


async def test_regenerated_password_invalidates_old(relay, host):
    old = host.session_password
    host.regenerate_password()
    with pytest.raises(AuthError):
        await make_viewer(relay, host, old).connect()


async def test_offline_target(relay):
    viewer = ViewerSession.relay(RelayAddress("127.0.0.1", relay.port), "123456789", "x")
    with pytest.raises(RelayError) as err:
        await viewer.connect()
    assert err.value.code == "offline"


async def test_relay_only_sees_ciphertext(relay, host, monkeypatch):
    """Everything the relay pipes after pairing must be opaque."""
    import teamlooker.relay as relay_mod
    seen = bytearray()
    original = relay_mod._pipe

    async def spy(reader, writer):
        class R:
            async def read(self, n):
                data = await reader.read(n)
                seen.extend(data)
                return data
        await original(R(), writer)

    monkeypatch.setattr(relay_mod, "_pipe", spy)
    inbox = Inbox()
    viewer = make_viewer(relay, host, host.session_password, on_message=inbox)
    await viewer.connect()
    runner = asyncio.create_task(viewer.run())
    await viewer.send({"type": "chat", "text": "TOP-SECRET-MESSAGE"})
    await wait_for(lambda: any(e["type"] == "chat" for e in host.events))
    assert b"TOP-SECRET-MESSAGE" not in seen
    assert host.session_password.encode() not in seen
    assert b"session_info" not in seen
    await viewer.close()
    await asyncio.wait_for(runner, 5)


async def test_session_survives_without_clipboard(relay, host):
    host._clipboard.available = False
    inbox = Inbox()
    viewer = make_viewer(relay, host, host.session_password, on_message=inbox)
    await viewer.connect()
    runner = asyncio.create_task(viewer.run())
    await wait_for(lambda: inbox.of("frame"))
    await asyncio.sleep(0.3)
    assert host.sessions and not runner.done()
    await viewer.close()
    await asyncio.wait_for(runner, 5)


async def test_session_survives_capture_failure(relay, host):
    class BrokenCapturer:
        def monitors(self):
            raise RuntimeError("no display")

        def grab(self, index):
            raise RuntimeError("no display")

        def close(self):
            pass

    host.capturer_factory = BrokenCapturer
    inbox = Inbox()
    viewer = make_viewer(relay, host, host.session_password, on_message=inbox)
    info = await viewer.connect()
    assert info["monitors"] == []
    runner = asyncio.create_task(viewer.run())
    await wait_for(lambda: inbox.of("screen_error"))
    await viewer.send({"type": "chat", "text": "still here"})
    await wait_for(lambda: any(e["type"] == "chat" for e in host.events))
    await viewer.send({"type": "mouse_move", "x": 0.5, "y": 0.5})  # ignored, no crash
    assert not runner.done()
    await viewer.close()
    await asyncio.wait_for(runner, 5)
