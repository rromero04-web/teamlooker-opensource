"""File manager operations and chunked, integrity-checked file transfers.

The same helpers serve both panes of the file manager: the viewer's local
pane and the host's pane (reached through the encrypted channel).

Transfer messages (in either direction)::

    file_begin  {tid, name, size, dest?}
    file_chunk  {tid} + raw bytes
    file_end    {tid, sha256}
    file_cancel {tid}
    file_result {tid, ok, path?, error?}   (sent back by the receiver)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import string
import sys
from pathlib import Path
from typing import Awaitable, Callable

CHUNK_SIZE = 64 * 1024
MAX_FILE_SIZE = 64 * 1024 ** 3


class FileOpError(Exception):
    pass


def _roots() -> list[str]:
    if sys.platform == "win32":
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return ["/"]


def list_dir(path: str | None) -> dict:
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
        if not target.is_dir():
            raise FileOpError(f"No es una carpeta: {target}")
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                    stat = entry.stat()
                    size = 0 if is_dir else stat.st_size
                    mtime = int(stat.st_mtime)
                except OSError:
                    is_dir, size, mtime = False, 0, 0
                entries.append({"name": entry.name, "dir": is_dir, "size": size, "mtime": mtime})
    except PermissionError as exc:
        raise FileOpError(f"Permiso denegado: {target}") from exc
    except FileNotFoundError as exc:
        raise FileOpError(f"No existe: {target}") from exc
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "entries": entries,
            "sep": os.sep, "roots": _roots(), "home": str(Path.home())}


def safe_name(name: str) -> str:
    """Reduce a peer-supplied file name to a harmless single path component."""
    name = str(name).replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
    if name in ("", ".", ".."):
        raise FileOpError("Nombre de archivo no válido")
    return name[:255]


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = os.path.splitext(name)
    n = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def fs_operation(op: str, args: dict) -> dict:
    """mkdir / rename / delete, shared by host and local panes."""
    try:
        if op == "mkdir":
            parent = Path(args["path"]).expanduser()
            target = parent / safe_name(args["name"])
            target.mkdir()
            return {"path": str(target)}
        if op == "rename":
            src = Path(args["path"]).expanduser()
            dst = src.parent / safe_name(args["name"])
            if dst.exists():
                raise FileOpError(f"Ya existe: {dst.name}")
            src.rename(dst)
            return {"path": str(dst)}
        if op == "delete":
            target = Path(args["path"]).expanduser()
            if target.is_dir() and not target.is_symlink():
                target.rmdir()  # only empty folders: no recursive deletes over the wire
            else:
                target.unlink()
            return {"path": str(target)}
    except FileOpError:
        raise
    except KeyError as exc:
        raise FileOpError(f"Falta el parámetro {exc}") from exc
    except OSError as exc:
        raise FileOpError(exc.strerror or str(exc)) from exc
    raise FileOpError(f"Operación desconocida: {op}")


class IncomingTransfer:
    def __init__(self, tid: str, directory: Path, name: str, size: int):
        if size < 0 or size > MAX_FILE_SIZE:
            raise FileOpError("Tamaño de archivo no válido")
        directory = directory.expanduser()
        if not directory.is_dir():
            raise FileOpError(f"La carpeta de destino no existe: {directory}")
        self.tid = tid
        self.size = size
        self.final_path = unique_path(directory, safe_name(name))
        self.part_path = self.final_path.with_name(self.final_path.name + ".tlpart")
        self.received = 0
        self._hash = hashlib.sha256()
        self._fh = open(self.part_path, "xb")

    def write(self, data: bytes) -> None:
        self.received += len(data)
        if self.received > self.size:
            raise FileOpError("Se recibieron más datos de los anunciados")
        self._hash.update(data)
        self._fh.write(data)

    def finish(self, sha256: str) -> Path:
        self._fh.close()
        if self.received != self.size or self._hash.hexdigest() != sha256:
            self.part_path.unlink(missing_ok=True)
            raise FileOpError("El archivo llegó incompleto o dañado")
        final = unique_path(self.final_path.parent, self.final_path.name)
        os.replace(self.part_path, final)
        return final

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            self.part_path.unlink(missing_ok=True)


class TransferReceiver:
    """Tracks incoming transfers for one session."""

    def __init__(self):
        self.transfers: dict[str, IncomingTransfer] = {}

    def begin(self, tid: str, directory: Path, name: str, size: int) -> IncomingTransfer:
        if tid in self.transfers:
            raise FileOpError("Transferencia duplicada")
        transfer = IncomingTransfer(tid, directory, name, size)
        self.transfers[tid] = transfer
        return transfer

    def chunk(self, tid: str, data: bytes) -> IncomingTransfer | None:
        transfer = self.transfers.get(tid)
        if transfer is None:
            return None
        try:
            transfer.write(data)
        except Exception:
            self.transfers.pop(tid, None)
            transfer.abort()
            raise
        return transfer

    def end(self, tid: str, sha256: str) -> Path:
        transfer = self.transfers.pop(tid, None)
        if transfer is None:
            raise FileOpError("Transferencia desconocida")
        return transfer.finish(sha256)

    def cancel(self, tid: str) -> None:
        transfer = self.transfers.pop(tid, None)
        if transfer:
            transfer.abort()

    def abort_all(self) -> None:
        for tid in list(self.transfers):
            self.cancel(tid)


ProgressCb = Callable[[int, int], Awaitable[None] | None]


async def send_file(send_msg, tid: str, path: Path, dest: str | None = None,
                    progress: ProgressCb | None = None,
                    cancelled: Callable[[], bool] = lambda: False) -> None:
    """Stream ``path`` through ``send_msg(msg, data)``."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileOpError(f"No es un archivo: {path}")
    size = path.stat().st_size
    begin = {"type": "file_begin", "tid": tid, "name": path.name, "size": size}
    if dest is not None:
        begin["dest"] = dest
    await send_msg(begin)
    digest = hashlib.sha256()
    sent = 0
    loop = asyncio.get_running_loop()
    with open(path, "rb") as fh:
        while True:
            if cancelled():
                await send_msg({"type": "file_cancel", "tid": tid})
                return
            data = await loop.run_in_executor(None, fh.read, CHUNK_SIZE)
            if not data:
                break
            digest.update(data)
            sent += len(data)
            await send_msg({"type": "file_chunk", "tid": tid}, data)
            if progress:
                result = progress(sent, size)
                if asyncio.iscoroutine(result):
                    await result
    await send_msg({"type": "file_end", "tid": tid, "sha256": digest.hexdigest()})
