"""Length-prefixed framing over asyncio streams.

Every frame is a 4-byte big-endian length followed by the payload. The relay
speaks JSON frames for its control protocol; once two peers are paired the
relay pipes raw bytes and the peers keep using the same framing end to end.
"""

from __future__ import annotations

import asyncio
import json

MAX_FRAME = 16 * 1024 * 1024


class FrameError(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    size = int.from_bytes(header, "big")
    if size > MAX_FRAME:
        raise FrameError(f"frame too large: {size} bytes")
    return await reader.readexactly(size)


def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > MAX_FRAME:
        raise FrameError(f"frame too large: {len(payload)} bytes")
    writer.write(len(payload).to_bytes(4, "big") + payload)


async def send_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    write_frame(writer, json.dumps(obj, separators=(",", ":")).encode())
    await writer.drain()


async def recv_json(reader: asyncio.StreamReader) -> dict:
    data = await read_frame(reader)
    try:
        obj = json.loads(data)
    except ValueError as exc:
        raise FrameError("invalid JSON frame") from exc
    if not isinstance(obj, dict):
        raise FrameError("JSON frame must be an object")
    return obj


async def close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
