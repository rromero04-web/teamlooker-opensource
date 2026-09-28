"""Screen capture and dirty-tile JPEG encoding.

Only the parts of the screen that changed since the previous frame are sent:
the frame is split into tiles, unchanged tiles are skipped, and horizontal
runs of changed tiles are merged into a single JPEG rectangle.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageChops

TILE = 64


@dataclass
class Monitor:
    left: int
    top: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


class ScreenCapturer:
    """Captures a monitor with ``mss``. Must always be used from the same thread."""

    def __init__(self):
        self._sct = None

    def _mss(self):
        if self._sct is None:
            import mss
            self._sct = mss.mss()
        return self._sct

    def monitors(self) -> list[Monitor]:
        mons = self._mss().monitors
        physical = mons[1:] or mons[:1]
        return [Monitor(m["left"], m["top"], m["width"], m["height"]) for m in physical]

    def grab(self, index: int) -> Image.Image:
        mons = self.monitors()
        mon = mons[index if 0 <= index < len(mons) else 0]
        shot = self._mss().grab(mon.as_dict())
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class TileEncoder:
    def __init__(self, quality: int = 70, scale: float = 1.0, grayscale: bool = False):
        self.quality = quality
        self.scale = scale
        self.grayscale = grayscale
        self._prev: Image.Image | None = None

    def configure(self, quality: int | None = None, scale: float | None = None,
                  grayscale: bool | None = None) -> None:
        if quality is not None:
            self.quality = max(10, min(95, int(quality)))
        if scale is not None:
            self.scale = max(0.2, min(1.0, float(scale)))
        if grayscale is not None:
            self.grayscale = bool(grayscale)
        self.reset()

    def reset(self) -> None:
        """Forget the previous frame so the next one is sent in full."""
        self._prev = None

    def _prepare(self, img: Image.Image) -> Image.Image:
        if self.scale < 0.999:
            size = (max(1, round(img.width * self.scale)), max(1, round(img.height * self.scale)))
            img = img.resize(size, Image.Resampling.BILINEAR)
        if self.grayscale:
            img = img.convert("L").convert("RGB")
        return img

    def _dirty_rects(self, img: Image.Image) -> list[tuple[int, int, int, int]]:
        prev = self._prev
        if prev is None or prev.size != img.size:
            return [(0, 0, img.width, img.height)]
        diff = ImageChops.difference(prev, img)
        bbox = diff.getbbox()
        if bbox is None:
            return []
        rects = []
        x0 = bbox[0] // TILE * TILE
        y0 = bbox[1] // TILE * TILE
        for ty in range(y0, bbox[3], TILE):
            th = min(TILE, img.height - ty)
            run_start = None
            run_end = 0
            for tx in range(x0, bbox[2], TILE):
                tw = min(TILE, img.width - tx)
                if diff.crop((tx, ty, tx + tw, ty + th)).getbbox() is not None:
                    if run_start is None:
                        run_start = tx
                    run_end = tx + tw
                elif run_start is not None:
                    rects.append((run_start, ty, run_end - run_start, th))
                    run_start = None
            if run_start is not None:
                rects.append((run_start, ty, run_end - run_start, th))
        return rects

    def encode(self, img: Image.Image) -> tuple[dict, bytes] | None:
        """Return ``(meta, jpeg_blob)`` for the changes, or ``None`` if nothing changed."""
        img = self._prepare(img)
        rects = self._dirty_rects(img)
        self._prev = img
        if not rects:
            return None
        out = bytearray()
        meta_rects = []
        for x, y, w, h in rects:
            buf = io.BytesIO()
            img.crop((x, y, x + w, y + h)).save(buf, "JPEG", quality=self.quality)
            data = buf.getvalue()
            meta_rects.append([x, y, w, h, len(data)])
            out += data
        return {"width": img.width, "height": img.height, "rects": meta_rects}, bytes(out)


def apply_frame(canvas: Image.Image | None, meta: dict, blob: bytes) -> Image.Image:
    """Reference decoder (used by tests and the CLI screenshot tool)."""
    size = (meta["width"], meta["height"])
    if canvas is None or canvas.size != size:
        canvas = Image.new("RGB", size)
    offset = 0
    for x, y, w, h, length in meta["rects"]:
        tile = Image.open(io.BytesIO(blob[offset:offset + length]))
        canvas.paste(tile, (x, y))
        offset += length
    return canvas
