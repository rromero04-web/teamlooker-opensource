"""Host side: stay reachable through the relay and serve remote sessions."""

from __future__ import annotations

import asyncio
import logging
import platform
import secrets
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from teamlooker import __version__
from teamlooker.clipboard import Clipboard
from teamlooker.config import Config
from teamlooker.files import (FileOpError, TransferReceiver, fs_operation, list_dir,
                              send_file)
from teamlooker.framing import close_writer, recv_json, send_json
from teamlooker.ids import device_id_from_public_key, generate_password
from teamlooker.inputctl import InputController
from teamlooker.relay_client import (RelayAddress, RelayError, accept_session,
                                     public_key_bytes, register_host)
from teamlooker.screen import ScreenCapturer, TileEncoder
from teamlooker.secure import AuthError, ChannelClosed, SecureChannel, Verifier, host_handshake
from teamlooker.srp import create_verifier

log = logging.getLogger("teamlooker.host")

MAX_CREDITS = 3
FREE_ATTEMPTS = 3
MAX_LOCK_SECONDS = 300
CLIPBOARD_POLL = 1.0


class HostService:
    def __init__(self, config: Config, relay: RelayAddress | None = None,
                 capturer_factory: Callable[[], ScreenCapturer] = ScreenCapturer,
                 input_controller: InputController | None = None,
                 clipboard: Clipboard | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.config = config
        self.relay = relay or RelayAddress.parse(config.relay)
        self.capturer_factory = capturer_factory
        self._input = input_controller
        self._clipboard = clipboard
        self.on_event = on_event or (lambda event: None)
        self.device_id = device_id_from_public_key(public_key_bytes(config.identity_key))
        self.status = "offline"
        self.status_detail = ""
        self.sessions: dict[str, HostSession] = {}
        self._failures = 0
        self._locked_until = 0.0
        self._control_writer: asyncio.StreamWriter | None = None
        self._stopped = False
        self._runner: asyncio.Task | None = None
        self.session_password = ""
        self._session_verifier: tuple[bytes, bytes] | None = None
        self.regenerate_password()

    # --- state exposed to UIs ----------------------------------------------------

    @property
    def input(self) -> InputController:
        if self._input is None:
            self._input = InputController()
        return self._input

    @property
    def clipboard(self) -> Clipboard:
        if self._clipboard is None:
            self._clipboard = Clipboard()
        return self._clipboard

    def emit(self, kind: str, **data) -> None:
        try:
            self.on_event({"type": kind, **data})
        except Exception:
            log.exception("event handler failed")

    def snapshot(self) -> dict:
        return {
            "id": self.device_id,
            "password": self.session_password,
            "status": self.status,
            "status_detail": self.status_detail,
            "relay": str(self.relay),
            "permanent_password": self.config.permanent_verifier is not None,
            "permissions": self.config.permissions,
            "sessions": [s.describe() for s in self.sessions.values()],
        }

    def regenerate_password(self) -> str:
        self.session_password = generate_password()
        self._session_verifier = create_verifier(self.session_password)
        self.emit("password", password=self.session_password)
        return self.session_password

    def verifiers(self) -> list[Verifier]:
        result = []
        if self._session_verifier:
            result.append(Verifier(*self._session_verifier, label="session"))
        permanent = self.config.permanent_verifier
        if permanent:
            result.append(Verifier(*permanent, label="permanent"))
        return result

    def lock_remaining(self) -> float:
        return max(0.0, self._locked_until - time.monotonic())

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= FREE_ATTEMPTS:
            delay = min(MAX_LOCK_SECONDS, 5 * 2 ** (self._failures - FREE_ATTEMPTS))
            self._locked_until = time.monotonic() + delay
            log.warning("%d failed login attempts, locked for %ss", self._failures, delay)
        self.emit("auth_failed", failures=self._failures, locked_for=round(self.lock_remaining()))

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status, self.status_detail = status, detail
        self.emit("status", status=status, detail=detail, id=self.device_id)

    # --- relay connection -------------------------------------------------------

    def set_relay(self, relay: RelayAddress) -> None:
        self.relay = relay
        self.config.relay = str(relay)
        self.reconnect()

    def reconnect(self) -> None:
        writer = self._control_writer
        if writer is not None:
            writer.close()

    async def run(self) -> None:
        backoff = 1
        while not self._stopped:
            self._set_status("connecting", str(self.relay))
            try:
                reader, writer, device_id = await register_host(self.relay, self.config.identity_key)
            except (OSError, asyncio.TimeoutError, RelayError, asyncio.IncompleteReadError) as exc:
                self._set_status("offline", str(exc) or exc.__class__.__name__)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)
                continue
            backoff = 1
            self.device_id = device_id
            self._control_writer = writer
            self._set_status("online", str(self.relay))
            log.info("online as %s via %s", device_id, self.relay)
            try:
                while True:
                    msg = await recv_json(reader)
                    kind = msg.get("type")
                    if kind == "ping":
                        await send_json(writer, {"type": "pong"})
                    elif kind == "incoming":
                        asyncio.create_task(self._accept(str(msg.get("session")),
                                                         str(msg.get("peer", ""))))
            except (OSError, asyncio.IncompleteReadError, ValueError):
                pass
            finally:
                self._control_writer = None
                await close_writer(writer)
            if not self._stopped:
                self._set_status("offline", "conexión con el servidor perdida")
                await asyncio.sleep(1)

    def start(self) -> asyncio.Task:
        self._runner = asyncio.create_task(self.run())
        return self._runner

    async def stop(self) -> None:
        self._stopped = True
        self.reconnect()
        for session in list(self.sessions.values()):
            await session.close()
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass

    async def _accept(self, session_id: str, peer: str) -> None:
        try:
            reader, writer = await accept_session(self.relay, session_id)
        except (OSError, asyncio.TimeoutError, RelayError, asyncio.IncompleteReadError):
            return
        try:
            channel, label = await host_handshake(reader, writer, self.verifiers(),
                                                  self.lock_remaining())
        except AuthError as exc:
            if exc.reason == "password":
                self._record_failure()
            await close_writer(writer)
            return
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            await close_writer(writer)
            return
        self._failures = 0
        session = HostSession(self, channel, peer, label)
        self.sessions[session.id] = session
        try:
            await session.run()
        finally:
            self.sessions.pop(session.id, None)
            self.emit("session_ended", session=session.id)

    # --- actions from the local UI -------------------------------------------

    async def send_chat(self, session_id: str, text: str) -> None:
        session = self.sessions.get(session_id)
        if session:
            await session.send({"type": "chat", "text": text[:4000]})

    async def disconnect(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session:
            await session.close("El anfitrión cerró la sesión")


class HostSession:
    def __init__(self, service: HostService, channel: SecureChannel, peer: str, label: str):
        self.service = service
        self.channel = channel
        self.peer = peer
        self.auth_label = label
        self.id = secrets.token_hex(4)
        self.viewer_name = "?"
        self.started = int(time.time())
        self.monitor = 0
        self.monitors: list = []
        self.fps = 20
        self.encoder = TileEncoder()
        self.receiver = TransferReceiver()
        self._capture_exec = ThreadPoolExecutor(1, thread_name_prefix="tl-capture")
        self._input_exec = ThreadPoolExecutor(1, thread_name_prefix="tl-input")
        self._capturer: ScreenCapturer | None = None
        self._credits = MAX_CREDITS
        self._credit_event = asyncio.Event()
        self._credit_event.set()
        self._uploads: dict[str, asyncio.Task] = {}
        self._clip_last: str | None = None
        self._closing = False
        self._cursor = None
        self.frames_sent = 0

    def describe(self) -> dict:
        return {"id": self.id, "viewer": self.viewer_name, "peer": self.peer,
                "started": self.started, "auth": self.auth_label}

    @property
    def perms(self) -> dict:
        return self.service.config.permissions

    async def send(self, msg: dict, data: bytes = b"") -> None:
        await self.channel.send_msg(msg, data)

    async def _in_capture_thread(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._capture_exec, fn, *args)

    def _capture(self):
        if self._capturer is None:
            self._capturer = self.service.capturer_factory()
        return self._capturer.grab(self.monitor)

    def _list_monitors(self):
        if self._capturer is None:
            self._capturer = self.service.capturer_factory()
        return self._capturer.monitors()

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            msg, _ = await asyncio.wait_for(self.channel.recv_msg(), 15)
            if msg.get("type") != "viewer_hello":
                return
            self.viewer_name = str(msg.get("name", "?"))[:64]
            try:
                self.monitors = await self._in_capture_thread(self._list_monitors)
            except Exception as exc:
                log.warning("cannot list monitors: %s", exc)
                self.monitors = []
            await self.send({
                "type": "session_info",
                "hostname": self.service.config.data.get("device_name") or socket.gethostname(),
                "os": f"{platform.system()} {platform.release()}",
                "version": __version__,
                "device_id": self.service.device_id,
                "monitors": [m.as_dict() for m in self.monitors],
                "permissions": self.perms,
                "input_available": self.service.input.available,
                "clipboard_available": self.service.clipboard.available,
            })
            self.service.emit("session_started", session=self.describe())
            log.info("session %s started (viewer %r, %s password)", self.id,
                     self.viewer_name, self.auth_label)
            # Only the receive loop decides when the session is over: the clipboard
            # watcher and the screen stream may stop early (no backend available).
            streaming = asyncio.create_task(self._frame_loop())
            streaming.add_done_callback(self._stream_done)
            receive = asyncio.create_task(self._recv_loop())
            tasks = [asyncio.create_task(self._clipboard_loop()), streaming, receive]
            await receive
        except (ChannelClosed, asyncio.TimeoutError, ValueError):
            pass
        except Exception:
            log.exception("session %s failed", self.id)
        finally:
            for task in tasks + list(self._uploads.values()):
                task.cancel()
            await self._cleanup()

    def _stream_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None \
                and not isinstance(task.exception(), ChannelClosed):
            log.error("session %s: screen streaming failed", self.id, exc_info=task.exception())

    async def _cleanup(self) -> None:
        self.receiver.abort_all()
        self._input_exec.submit(self.service.input.release_all)
        self._input_exec.shutdown(wait=False)
        if self._capturer is not None:
            try:
                await self._in_capture_thread(self._capturer.close)
            except Exception:
                pass
        self._capture_exec.shutdown(wait=False)
        await self.channel.close()
        log.info("session %s ended", self.id)

    async def close(self, reason: str = "") -> None:
        if self._closing:
            return
        self._closing = True
        try:
            await self.send({"type": "bye", "reason": reason})
        except ChannelClosed:
            pass
        await self.channel.close()

    # --- screen streaming ------------------------------------------------------

    def _cursor_position(self) -> list[float] | None:
        pos = self.service.input.position() if self.service.input.available else None
        if pos is None or not self.monitors:
            return None
        mon = self.monitors[min(self.monitor, len(self.monitors) - 1)]
        x = (pos[0] - mon.left) / max(1, mon.width)
        y = (pos[1] - mon.top) / max(1, mon.height)
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        return [round(x, 4), round(y, 4)]

    async def _frame_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self._credit_event.wait()
            started = loop.time()
            try:
                img = await self._in_capture_thread(self._capture)
            except Exception as exc:
                # No display / unsupported session: keep chat and files usable.
                log.warning("screen capture failed: %s", exc)
                await self.send({"type": "screen_error",
                                 "message": f"No se puede capturar la pantalla: {exc}"})
                return
            encoded = await self._in_capture_thread(self.encoder.encode, img)
            cursor = self._cursor_position()
            if encoded is not None:
                meta, blob = encoded
                meta.update(type="frame", cursor=cursor, monitor=self.monitor)
                self._credits -= 1
                if self._credits <= 0:
                    self._credit_event.clear()
                await self.send(meta, blob)
                self.frames_sent += 1
            elif cursor != self._cursor:
                await self.send({"type": "cursor", "cursor": cursor})
            self._cursor = cursor
            await asyncio.sleep(max(0.0, 1 / self.fps - (loop.time() - started)))

    def _grant_credit(self) -> None:
        self._credits = min(MAX_CREDITS, self._credits + 1)
        self._credit_event.set()

    # --- clipboard ----------------------------------------------------------------

    async def _clipboard_loop(self) -> None:
        clipboard = self.service.clipboard
        if not clipboard.available:
            return
        loop = asyncio.get_running_loop()
        self._clip_last = await loop.run_in_executor(None, clipboard.get)
        while True:
            await asyncio.sleep(CLIPBOARD_POLL)
            if not self.perms.get("clipboard"):
                continue
            text = await loop.run_in_executor(None, clipboard.get)
            if text is not None and text != self._clip_last:
                self._clip_last = text
                await self.send({"type": "clipboard", "text": text})

    # --- incoming messages ---------------------------------------------------

    def _to_screen(self, msg: dict) -> tuple[int, int] | None:
        try:
            x, y = float(msg["x"]), float(msg["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not self.monitors:
            return None
        mon = self.monitors[min(self.monitor, len(self.monitors) - 1)]
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        return (mon.left + round(x * (mon.width - 1)), mon.top + round(y * (mon.height - 1)))

    def _inject(self, fn, *args) -> None:
        if self.perms.get("control"):
            self._input_exec.submit(fn, *args)

    async def _recv_loop(self) -> None:
        while True:
            msg, data = await self.channel.recv_msg()
            kind = msg.get("type")
            try:
                await self._dispatch(kind, msg, data)
            except ChannelClosed:
                raise
            except Exception as exc:
                log.warning("error handling %s: %s", kind, exc)
            if kind == "bye":
                return

    async def _dispatch(self, kind: str, msg: dict, data: bytes) -> None:
        inp = self.service.input
        if kind == "frame_ack":
            self._grant_credit()
        elif kind == "mouse_move":
            pos = self._to_screen(msg)
            if pos:
                self._inject(inp.move, *pos)
        elif kind == "mouse_button":
            pos = self._to_screen(msg)
            if pos:
                self._inject(inp.move, *pos)
            self._inject(inp.button, int(msg.get("button", 0)), bool(msg.get("down")))
        elif kind == "mouse_scroll":
            self._inject(inp.scroll, int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif kind == "key":
            self._inject(inp.key, str(msg.get("key", "")), str(msg.get("code", "")),
                         bool(msg.get("down")))
        elif kind == "combo":
            keys = [str(k) for k in msg.get("keys", [])][:6]
            self._inject(inp.combo, keys)
        elif kind == "type_text":
            self._inject(inp.type_text, str(msg.get("text", ""))[:10000])
        elif kind == "refresh":
            await self._in_capture_thread(self.encoder.reset)
        elif kind == "settings":
            if "fps" in msg:
                self.fps = max(1, min(60, int(msg["fps"])))
            await self._in_capture_thread(lambda: self.encoder.configure(
                msg.get("quality"), msg.get("scale"), msg.get("grayscale")))
        elif kind == "monitor":
            index = int(msg.get("index", 0))
            if 0 <= index < len(self.monitors):
                self.monitor = index
                await self._in_capture_thread(self.encoder.reset)
        elif kind == "chat":
            self.service.emit("chat", session=self.id, sender=self.viewer_name,
                              text=str(msg.get("text", ""))[:4000])
        elif kind == "clipboard":
            if self.perms.get("clipboard") and isinstance(msg.get("text"), str):
                self._clip_last = msg["text"]
                await asyncio.get_running_loop().run_in_executor(
                    None, self.service.clipboard.set, msg["text"])
        elif kind in ("fs_list", "fs_op"):
            await self._handle_fs(kind, msg)
        elif kind in ("file_begin", "file_chunk", "file_end", "file_cancel", "file_get"):
            await self._handle_transfer(kind, msg, data)
        elif kind == "bye":
            await self.channel.close()

    async def _handle_fs(self, kind: str, msg: dict) -> None:
        reply = {"type": "fs_result", "req": msg.get("req")}
        if not self.perms.get("files"):
            reply.update(ok=False, error="El anfitrión no permite el acceso a archivos")
        else:
            try:
                loop = asyncio.get_running_loop()
                if kind == "fs_list":
                    result = await loop.run_in_executor(None, list_dir, msg.get("path"))
                else:
                    result = await loop.run_in_executor(None, fs_operation, str(msg.get("op")),
                                                        dict(msg.get("args") or {}))
                reply.update(ok=True, data=result)
            except FileOpError as exc:
                reply.update(ok=False, error=str(exc))
        await self.send(reply)

    async def _handle_transfer(self, kind: str, msg: dict, data: bytes) -> None:
        tid = str(msg.get("tid", ""))
        if kind == "file_begin":
            if not self.perms.get("files"):
                await self.send({"type": "file_result", "tid": tid, "ok": False,
                                 "error": "El anfitrión no permite transferir archivos"})
                return
            try:
                self.receiver.begin(tid, Path(str(msg.get("dest") or Path.home())),
                                    str(msg.get("name", "")), int(msg.get("size", -1)))
            except (FileOpError, OSError, ValueError) as exc:
                await self.send({"type": "file_result", "tid": tid, "ok": False, "error": str(exc)})
        elif kind == "file_chunk":
            try:
                self.receiver.chunk(tid, data)
            except (FileOpError, OSError) as exc:
                await self.send({"type": "file_result", "tid": tid, "ok": False, "error": str(exc)})
        elif kind == "file_end":
            try:
                path = self.receiver.end(tid, str(msg.get("sha256", "")))
                self.service.emit("file_received", session=self.id, path=str(path))
                await self.send({"type": "file_result", "tid": tid, "ok": True, "path": str(path)})
            except (FileOpError, OSError) as exc:
                await self.send({"type": "file_result", "tid": tid, "ok": False, "error": str(exc)})
        elif kind == "file_cancel":
            self.receiver.cancel(tid)
            task = self._uploads.pop(tid, None)
            if task:
                task.cancel()
        elif kind == "file_get":
            if not self.perms.get("files"):
                await self.send({"type": "file_result", "tid": tid, "ok": False,
                                 "error": "El anfitrión no permite transferir archivos"})
                return
            self._uploads[tid] = asyncio.create_task(self._send_file(tid, str(msg.get("path", ""))))

    async def _send_file(self, tid: str, path: str) -> None:
        try:
            await send_file(self.send, tid, Path(path))
        except (FileOpError, OSError) as exc:
            await self.send({"type": "file_result", "tid": tid, "ok": False, "error": str(exc)})
        finally:
            self._uploads.pop(tid, None)
