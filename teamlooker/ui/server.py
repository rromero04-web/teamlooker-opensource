"""Local desktop UI served to the user's browser on 127.0.0.1.

The Python process keeps the host service running (so this computer can be
reached) and owns every remote session; the browser only renders. A random
token in the URL protects the local API from other local users and websites.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web

from teamlooker import __version__
from teamlooker.clipboard import Clipboard
from teamlooker.config import Config
from teamlooker.files import FileOpError, fs_operation, list_dir
from teamlooker.host import HostService
from teamlooker.ids import normalize_id
from teamlooker.messages import pack
from teamlooker.relay_client import RelayAddress, RelayError
from teamlooker.secure import AuthError, ChannelClosed
from teamlooker.viewer import ViewerSession

log = logging.getLogger("teamlooker.ui")

STATIC = Path(__file__).parent / "static"

AUTH_ERRORS = {
    "password": "Contraseña incorrecta.",
    "locked": "Demasiados intentos fallidos. El equipo remoto bloqueó el acceso durante {s} s.",
    "no_password": "El equipo remoto no acepta conexiones (sin contraseña).",
    "protocol": "Versión de protocolo incompatible.",
    "host_verification": "No se pudo verificar la identidad del equipo remoto.",
}


class UIServer:
    def __init__(self, config: Config, host: HostService | None, port: int = 0,
                 token: str | None = None):
        self.config = config
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.main_sockets: set[web.WebSocketResponse] = set()
        self.viewers: set[ViewerSession] = set()
        # Incoming-session chat history, kept here so a freshly opened window sees it.
        self.chats: dict[str, list[dict]] = {}
        self.clipboard = Clipboard()
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/viewer", self._viewer_page),
            web.get("/ws/main", self._ws_main),
            web.get("/ws/viewer", self._ws_viewer),
            web.static("/static", STATIC),
        ])
        self._runner: web.AppRunner | None = None
        if host is not None:
            host.on_event = self._on_host_event

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for viewer in list(self.viewers):
            await viewer.close()
        for ws in list(self.main_sockets):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    def open_browser(self) -> None:
        webbrowser.open(self.url)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path.startswith("/static/"):
            return await handler(request)
        if not secrets.compare_digest(request.query.get("token", ""), self.token):
            raise web.HTTPForbidden(text="TeamLooker: token inválido")
        response = await handler(request)
        if isinstance(response, web.FileResponse):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' blob: data:; "
                "connect-src 'self'; style-src 'self' 'unsafe-inline'")
        return response

    async def _index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def _viewer_page(self, request):
        return web.FileResponse(STATIC / "viewer.html")

    # --- main window --------------------------------------------------------

    def _state(self) -> dict:
        host = self.host.snapshot() if self.host else None
        return {"type": "state", "version": __version__, "host": host, "chats": self.chats,
                "recent": self.config.recent, "relay": self.config.relay,
                "device_name": self.config.data.get("device_name", "")}

    def _broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.main_sockets):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(data))

    def _on_host_event(self, event: dict) -> None:
        if event["type"] == "chat":
            self.chats.setdefault(event["session"], []).append(
                {"who": event["sender"], "text": event["text"], "me": False})
        elif event["type"] == "session_ended":
            self.chats.pop(event["session"], None)
        self._broadcast({"type": "host_event", "event": event})
        self._broadcast(self._state())

    async def _ws_main(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.main_sockets.add(ws)
        await ws.send_json(self._state())
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(raw.data)
                    reply = await self._main_command(msg)
                except (ValueError, KeyError, TypeError) as exc:
                    reply = {"type": "error", "message": f"Petición no válida: {exc}"}
                if reply:
                    await ws.send_json(reply)
                self._broadcast(self._state())
        finally:
            self.main_sockets.discard(ws)
        return ws

    async def _main_command(self, msg: dict) -> dict | None:
        kind = msg.get("type")
        host = self.host
        if kind == "regenerate_password" and host:
            host.regenerate_password()
        elif kind == "set_permanent_password":
            password = str(msg.get("password") or "")
            if password and len(password) < 8:
                return {"type": "error", "message": "La contraseña permanente debe tener al menos 8 caracteres."}
            self.config.set_permanent_password(password or None)
            return {"type": "notice", "message": "Contraseña permanente guardada." if password
                    else "Acceso desatendido desactivado."}
        elif kind == "set_permissions":
            self.config.set_permissions(**{k: bool(v) for k, v in dict(msg["permissions"]).items()})
        elif kind == "set_relay":
            relay = RelayAddress.parse(str(msg["relay"]))
            if host:
                host.set_relay(relay)
            else:
                self.config.relay = str(relay)
        elif kind == "set_device_name":
            self.config.data["device_name"] = str(msg.get("name", ""))[:64]
            self.config.save()
        elif kind == "host_chat" and host:
            session, text = str(msg["session"]), str(msg["text"])[:4000]
            if session in host.sessions:
                await host.send_chat(session, text)
                self.chats.setdefault(session, []).append({"who": "Tú", "text": text, "me": True})
        elif kind == "host_disconnect" and host:
            await host.disconnect(str(msg["session"]))
        elif kind == "remove_recent":
            self.config.remove_recent(str(msg["id"]))
        return None

    # --- remote control window --------------------------------------------------

    async def _ws_viewer(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        viewer: ViewerSession | None = None
        runner: asyncio.Task | None = None
        try:
            first = await ws.receive_json(timeout=600)
            if first.get("type") != "connect":
                return ws
            partner = normalize_id(first.get("partner", ""))
            viewer = await self._connect(ws, partner, str(first.get("password", "")))
            if viewer is None:
                return ws
            runner = asyncio.create_task(self._pump_viewer(ws, viewer))
            async for raw in ws:
                if raw.type == WSMsgType.TEXT:
                    await self._viewer_command(ws, viewer, json.loads(raw.data))
        except (ChannelClosed, ConnectionError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            if viewer is not None:
                await viewer.close()
                self.viewers.discard(viewer)
            if runner:
                try:
                    await asyncio.wait_for(runner, 3)
                except (asyncio.TimeoutError, Exception):
                    runner.cancel()
            await ws.close()
        return ws

    async def _connect(self, ws, partner: str, password: str) -> ViewerSession | None:
        if len(partner) != 9:
            await ws.send_json({"type": "connect_error", "message": "El ID debe tener 9 dígitos."})
            return None

        async def forward(msg: dict, data: bytes) -> None:
            if ws.closed:
                return
            if msg.get("type") == "frame":
                await ws.send_bytes(pack(msg, data))
            else:
                await ws.send_json(msg)

        viewer = ViewerSession(RelayAddress.parse(self.config.relay), partner, password,
                               name=self.config.data.get("device_name", "viewer"),
                               on_message=forward, clipboard=self.clipboard)
        await ws.send_json({"type": "connect_progress", "message": "Conectando con el servidor…"})
        try:
            info = await viewer.connect()
        except AuthError as exc:
            text = AUTH_ERRORS.get(exc.reason, f"Autenticación fallida ({exc.reason}).")
            await ws.send_json({"type": "connect_error", "message": text.format(s=exc.retry_after),
                                "reason": exc.reason})
            return None
        except RelayError as exc:
            await ws.send_json({"type": "connect_error", "message": str(exc)})
            return None
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            await ws.send_json({"type": "connect_error",
                                "message": f"No se pudo contactar con el servidor {self.config.relay}: "
                                           f"{exc.__class__.__name__}"})
            return None
        self.viewers.add(viewer)
        self.config.add_recent(partner, info.get("hostname", ""))
        self._broadcast(self._state())
        await ws.send_json({"type": "connected", "info": info,
                            "download_dir": str(viewer.download_dir),
                            "clipboard_available": self.clipboard.available})
        return viewer

    async def _pump_viewer(self, ws, viewer: ViewerSession) -> None:
        try:
            await viewer.run()
        finally:
            if not ws.closed:
                try:
                    await ws.send_json({"type": "disconnected"})
                except ConnectionError:
                    pass
                await ws.close()

    async def _viewer_command(self, ws, viewer: ViewerSession, msg: dict) -> None:
        kind = msg.get("type")
        forwardable = {"frame_ack", "mouse_move", "mouse_button", "mouse_scroll", "key", "combo",
                       "type_text", "refresh", "settings", "monitor", "chat", "clipboard"}
        if kind in forwardable:
            await viewer.send(msg)
        elif kind == "clipboard_sync":
            viewer.clipboard_sync = bool(msg.get("enabled"))
        elif kind == "remote_fs":
            request = dict(msg.get("request") or {})
            if request.get("type") not in ("fs_list", "fs_op"):
                return
            try:
                result = await viewer.request(request)
            except asyncio.TimeoutError:
                result = {"ok": False, "error": "Sin respuesta del equipo remoto"}
            await ws.send_json({"type": "fs_reply", "ui_req": msg.get("ui_req"), "side": "remote",
                                "ok": result.get("ok"), "data": result.get("data"),
                                "error": result.get("error")})
        elif kind == "local_fs":
            loop = asyncio.get_running_loop()
            try:
                if msg.get("op") == "list":
                    data = await loop.run_in_executor(None, list_dir, msg.get("path"))
                else:
                    data = await loop.run_in_executor(None, fs_operation, str(msg.get("op")),
                                                      dict(msg.get("args") or {}))
                reply = {"ok": True, "data": data}
            except FileOpError as exc:
                reply = {"ok": False, "error": str(exc)}
            await ws.send_json({"type": "fs_reply", "ui_req": msg.get("ui_req"),
                                "side": "local", **reply})
        elif kind == "upload":
            tid = viewer.upload(str(msg["path"]), str(msg["dest"]))
            await ws.send_json({"type": "transfer_started", "tid": tid, "direction": "up",
                                "name": Path(str(msg["path"])).name})
        elif kind == "download":
            tid = await viewer.download(str(msg["path"]), msg.get("dest"))
            await ws.send_json({"type": "transfer_started", "tid": tid, "direction": "down",
                                "name": Path(str(msg["path"])).name})
        elif kind == "cancel_transfer":
            await viewer.cancel_transfer(str(msg["tid"]))
        elif kind == "disconnect":
            await viewer.close()


async def run_app(config: Config, port: int = 0, open_browser: bool = True,
                  enable_host: bool = True) -> None:
    host = HostService(config) if enable_host else None
    ui = UIServer(config, host, port)
    await ui.start()
    if host:
        host.start()
    print(f"TeamLooker {__version__}")
    if host:
        print(f"  Tu ID:        {host.device_id}")
        print(f"  Contraseña:   {host.session_password}")
    print(f"  Servidor:     {config.relay}")
    print(f"  Interfaz:     {ui.url}")
    if open_browser:
        ui.open_browser()
    try:
        await asyncio.Event().wait()
    finally:
        if host:
            await host.stop()
        await ui.stop()
