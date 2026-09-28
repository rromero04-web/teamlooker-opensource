import pytest
from aiohttp.test_utils import TestClient, TestServer

from teamlooker.config import Config
from teamlooker.ui.server import UIServer


@pytest.fixture
async def ui(relay, host, tmp_path):
    config = Config(tmp_path / "ui-config.json")
    config.data["relay"] = f"127.0.0.1:{relay.port}"
    server = UIServer(config, None, token="secret-token")
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield server, client
    await client.close()


async def test_token_required(ui):
    _, client = ui
    assert (await client.get("/")).status == 403
    assert (await client.get("/?token=wrong")).status == 403
    resp = await client.get("/?token=secret-token")
    assert resp.status == 200 and "TeamLooker" in await resp.text()
    assert "Content-Security-Policy" in resp.headers
    assert (await client.get("/static/style.css")).status == 200


async def test_main_socket_state(ui):
    _, client = ui
    ws = await client.ws_connect("/ws/main?token=secret-token")
    state = await ws.receive_json()
    assert state["type"] == "state" and state["host"] is None
    await ws.send_json({"type": "set_permanent_password", "password": "short"})
    assert (await ws.receive_json())["type"] == "error"
    await ws.close()


async def test_viewer_socket_connects_and_streams(ui, host):
    server, client = ui
    ws = await client.ws_connect("/ws/viewer?token=secret-token")
    await ws.send_json({"type": "connect", "partner": host.device_id, "password": "nope"})
    msgs = [await ws.receive_json(), await ws.receive_json()]
    assert msgs[-1]["type"] == "connect_error" and "incorrecta" in msgs[-1]["message"]

    ws = await client.ws_connect("/ws/viewer?token=secret-token")
    await ws.send_json({"type": "connect", "partner": host.device_id,
                        "password": host.session_password})
    while (msg := await ws.receive_json())["type"] != "connected":
        assert msg["type"] == "connect_progress"
    assert msg["info"]["device_id"] == host.device_id
    frame = await ws.receive_bytes()
    assert b'"type":"frame"' in frame
    assert server.config.recent[0]["id"] == host.device_id
    await ws.send_json({"type": "remote_fs", "ui_req": 7,
                        "request": {"type": "fs_list", "path": ""}})
    while (msg := await ws.receive())[0].name != "TEXT" or '"fs_reply"' not in msg.data:
        pass
    reply = msg.json()
    assert reply["ui_req"] == 7 and reply["ok"]
    await ws.close()
