import asyncio

import pytest
from PIL import Image, ImageDraw

from teamlooker.config import Config
from teamlooker.host import HostService
from teamlooker.relay import RelayServer
from teamlooker.relay_client import RelayAddress
from teamlooker.screen import Monitor


class FakeCapturer:
    """A 320x200 'screen' whose content tests can change."""

    frame_no = 0

    def monitors(self):
        return [Monitor(0, 0, 320, 200), Monitor(320, 0, 160, 100)]

    def grab(self, index):
        mon = self.monitors()[index]
        img = Image.new("RGB", (mon.width, mon.height), (30, 60, 90))
        ImageDraw.Draw(img).rectangle((10, 10, 40 + FakeCapturer.frame_no % 50, 40), fill=(250, 250, 0))
        return img

    def close(self):
        pass


class FakeInput:
    available = True

    def __init__(self):
        self.events = []

    def position(self):
        return (160, 100)

    def move(self, x, y):
        self.events.append(("move", x, y))

    def button(self, button, down):
        self.events.append(("button", button, down))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def key(self, key, code, down):
        self.events.append(("key", key, code, down))

    def combo(self, keys):
        self.events.append(("combo", tuple(keys)))

    def type_text(self, text):
        self.events.append(("type", text))

    def release_all(self):
        self.events.append(("release_all",))


class FakeClipboard:
    available = True

    def __init__(self):
        self.text = ""

    def get(self):
        return self.text

    def set(self, text):
        self.text = text


@pytest.fixture
def tl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMLOOKER_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
async def relay():
    server = RelayServer("127.0.0.1", 0)
    await server.start()
    yield server
    await server.stop()


async def _start(service):
    service.start()
    for _ in range(150):
        if service.status == "online":
            break
        await asyncio.sleep(0.02)
    assert service.status == "online", service.status_detail


@pytest.fixture
async def host(relay, tmp_path):
    events = []
    config = Config(tmp_path / "host-config.json")
    config.mode = "relay"
    service = HostService(config, RelayAddress("127.0.0.1", relay.port),
                          capturer_factory=FakeCapturer, input_controller=FakeInput(),
                          clipboard=FakeClipboard(), on_event=events.append)
    service.events = events
    await _start(service)
    yield service
    await service.stop()


@pytest.fixture
async def direct_host(tmp_path):
    """A host in direct mode (UPnP disabled) listening on a random local port."""
    events = []
    config = Config(tmp_path / "direct-config.json")
    config.mode = "direct"
    config.direct_port = 0  # let the OS pick a free port
    config.enable_upnp = False
    service = HostService(config, capturer_factory=FakeCapturer, input_controller=FakeInput(),
                          clipboard=FakeClipboard(), on_event=events.append)
    service.events = events
    await _start(service)
    yield service
    await service.stop()
