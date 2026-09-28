"""Viewer side: connect to a partner ID and drive a remote session."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from teamlooker.clipboard import Clipboard
from teamlooker.config import default_download_dir
from teamlooker.files import FileOpError, TransferReceiver, send_file
from teamlooker.framing import close_writer
from teamlooker.ids import normalize_id
from teamlooker.relay_client import RelayAddress, connect_to_host
from teamlooker.secure import ChannelClosed, SecureChannel, viewer_handshake

log = logging.getLogger("teamlooker.viewer")

CLIPBOARD_POLL = 1.0

MessageHandler = Callable[[dict, bytes], Awaitable[None]]


class ViewerSession:
    def __init__(self, relay: RelayAddress, partner_id: str, password: str,
                 name: str = "viewer", on_message: MessageHandler | None = None,
                 clipboard: Clipboard | None = None,
                 download_dir: Path | None = None):
        self.relay = relay
        self.partner_id = normalize_id(partner_id)
        self._password = password
        self.name = name
        self.on_message = on_message
        self.clipboard = clipboard
        self.download_dir = download_dir or default_download_dir()
        self.channel: SecureChannel | None = None
        self.info: dict = {}
        self.clipboard_sync = True
        self.receiver = TransferReceiver()
        self._download_dirs: dict[str, Path] = {}
        self._uploads: dict[str, asyncio.Task] = {}
        self._clip_last: str | None = None
        self._pending: dict[str, asyncio.Future] = {}

    async def connect(self) -> dict:
        reader, writer = await connect_to_host(self.relay, self.partner_id)
        try:
            self.channel = await viewer_handshake(reader, writer, self._password)
        except BaseException:
            await close_writer(writer)
            raise
        finally:
            self._password = ""
        await self.channel.send_msg({"type": "viewer_hello", "name": self.name})
        msg, _ = await asyncio.wait_for(self.channel.recv_msg(), 20)
        if msg.get("type") != "session_info":
            await self.channel.close()
            raise ConnectionError("respuesta inesperada del anfitrión")
        self.info = msg
        return msg

    async def send(self, msg: dict, data: bytes = b"") -> None:
        if self.channel is None:
            raise ChannelClosed()
        await self.channel.send_msg(msg, data)

    async def request(self, msg: dict, timeout: float = 30) -> dict:
        """Send a request carrying a ``req`` id and wait for its ``fs_result``."""
        req = secrets.token_hex(6)
        future = asyncio.get_running_loop().create_future()
        self._pending[req] = future
        try:
            await self.send({**msg, "req": req})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(req, None)

    async def run(self) -> None:
        clip_task = asyncio.create_task(self._clipboard_loop())
        try:
            while True:
                msg, data = await self.channel.recv_msg()
                kind = msg.get("type")
                if kind == "fs_result" and msg.get("req") in self._pending:
                    future = self._pending[msg["req"]]
                    if not future.done():
                        future.set_result(msg)
                    continue
                if kind in ("file_begin", "file_chunk", "file_end", "file_cancel", "file_result"):
                    await self._handle_transfer(kind, msg, data)
                    continue
                if kind == "clipboard" and isinstance(msg.get("text"), str):
                    self._clip_last = msg["text"]
                    if self.clipboard and self.clipboard_sync:
                        await asyncio.get_running_loop().run_in_executor(
                            None, self.clipboard.set, msg["text"])
                if self.on_message:
                    await self.on_message(msg, data)
                if kind == "bye":
                    break
        except ChannelClosed:
            pass
        finally:
            clip_task.cancel()
            for task in self._uploads.values():
                task.cancel()
            self.receiver.abort_all()
            if self.channel:
                await self.channel.close()

    async def close(self) -> None:
        if self.channel and not self.channel.closed:
            try:
                await self.send({"type": "bye"})
            except ChannelClosed:
                pass
            await self.channel.close()

    async def _emit(self, msg: dict) -> None:
        if self.on_message:
            await self.on_message(msg, b"")

    # --- clipboard ----------------------------------------------------------------

    async def _clipboard_loop(self) -> None:
        if not self.clipboard or not self.clipboard.available:
            return
        if not self.info.get("permissions", {}).get("clipboard"):
            return
        loop = asyncio.get_running_loop()
        self._clip_last = await loop.run_in_executor(None, self.clipboard.get)
        while True:
            await asyncio.sleep(CLIPBOARD_POLL)
            if not self.clipboard_sync:
                continue
            text = await loop.run_in_executor(None, self.clipboard.get)
            if text is not None and text != self._clip_last:
                self._clip_last = text
                await self.send({"type": "clipboard", "text": text})

    # --- file transfers -------------------------------------------------------

    def upload(self, local_path: str, remote_dir: str) -> str:
        tid = secrets.token_hex(6)
        path = Path(local_path).expanduser()

        async def progress(done: int, total: int) -> None:
            await self._emit({"type": "transfer_progress", "tid": tid, "direction": "up",
                              "name": path.name, "done": done, "total": total})

        async def runner() -> None:
            try:
                await send_file(self.send, tid, path, dest=remote_dir, progress=progress)
            except (FileOpError, OSError) as exc:
                await self._emit({"type": "transfer_done", "tid": tid, "ok": False,
                                  "direction": "up", "error": str(exc)})
            finally:
                self._uploads.pop(tid, None)

        self._uploads[tid] = asyncio.create_task(runner())
        return tid

    async def download(self, remote_path: str, local_dir: str | None = None) -> str:
        tid = secrets.token_hex(6)
        self._download_dirs[tid] = Path(local_dir).expanduser() if local_dir else self.download_dir
        await self.send({"type": "file_get", "tid": tid, "path": remote_path})
        return tid

    async def cancel_transfer(self, tid: str) -> None:
        task = self._uploads.pop(tid, None)
        if task:
            task.cancel()
        self.receiver.cancel(tid)
        self._download_dirs.pop(tid, None)
        await self.send({"type": "file_cancel", "tid": tid})
        await self._emit({"type": "transfer_done", "tid": tid, "ok": False, "error": "Cancelado"})

    async def _handle_transfer(self, kind: str, msg: dict, data: bytes) -> None:
        tid = str(msg.get("tid", ""))
        try:
            if kind == "file_begin":
                directory = self._download_dirs.pop(tid, None)
                if directory is None:  # we never asked for this file
                    await self.send({"type": "file_cancel", "tid": tid})
                    return
                directory.mkdir(parents=True, exist_ok=True)
                transfer = self.receiver.begin(tid, directory, str(msg.get("name", "")),
                                               int(msg.get("size", -1)))
                await self._emit({"type": "transfer_progress", "tid": tid, "direction": "down",
                                  "name": transfer.final_path.name, "done": 0,
                                  "total": transfer.size})
            elif kind == "file_chunk":
                transfer = self.receiver.chunk(tid, data)
                if transfer is not None:
                    await self._emit({"type": "transfer_progress", "tid": tid,
                                      "direction": "down", "name": transfer.final_path.name,
                                      "done": transfer.received, "total": transfer.size})
            elif kind == "file_end":
                path = self.receiver.end(tid, str(msg.get("sha256", "")))
                await self._emit({"type": "transfer_done", "tid": tid, "ok": True,
                                  "direction": "down", "path": str(path)})
            elif kind == "file_cancel":
                self.receiver.cancel(tid)
                await self._emit({"type": "transfer_done", "tid": tid, "ok": False,
                                  "error": "Cancelado por el anfitrión"})
            elif kind == "file_result":
                if not msg.get("ok"):
                    self.receiver.cancel(tid)
                    self._download_dirs.pop(tid, None)
                    task = self._uploads.pop(tid, None)
                    if task:
                        task.cancel()
                await self._emit({"type": "transfer_done", "tid": tid, "ok": bool(msg.get("ok")),
                                  "path": msg.get("path"),
                                  "error": msg.get("error")})
        except (FileOpError, OSError, ValueError) as exc:
            self.receiver.cancel(tid)
            await self.send({"type": "file_cancel", "tid": tid})
            await self._emit({"type": "transfer_done", "tid": tid, "ok": False, "error": str(exc)})
