"""Generate the TeamLooker logo/icon assets (PNG, ICO) with Pillow.

The mark: a rounded-square badge with a blue gradient, a white monitor, and
inside it an eye/lens — the "looker" watching a remote screen.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent.parent / "teamlooker" / "ui" / "static"
ASSETS = Path(__file__).parent

BG_TOP = (37, 140, 232)     # #258ce8
BG_BOTTOM = (11, 92, 200)   # #0b5cc8
WHITE = (255, 255, 255)
IRIS = (11, 111, 214)
PUPIL = (14, 34, 60)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    return mask


def _gradient(size: int) -> Image.Image:
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        # slight diagonal feel
        for x in range(size):
            tt = min(1.0, t * 0.82 + (x / (size - 1)) * 0.18)
            px[x, y] = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * tt) for i in range(3))
    return grad


def render(size: int) -> Image.Image:
    img = _gradient(size).convert("RGBA")
    img.putalpha(_rounded_mask(size, round(size * 0.22)))
    d = ImageDraw.Draw(img)
    s = size / 512.0

    # monitor body
    mx0, my0, mx1, my1 = 104 * s, 128 * s, 408 * s, 336 * s
    d.rounded_rectangle((mx0, my0, mx1, my1), radius=int(26 * s), fill=WHITE)
    # stand
    d.rectangle((236 * s, my1 - 2, 276 * s, 372 * s), fill=WHITE)
    d.rounded_rectangle((196 * s, 368 * s, 316 * s, 392 * s), radius=int(12 * s), fill=WHITE)
    # inner screen
    ix0, iy0, ix1, iy1 = 128 * s, 152 * s, 384 * s, 312 * s
    d.rounded_rectangle((ix0, iy0, ix1, iy1), radius=int(16 * s), fill=(226, 240, 253))

    # eye / lens centred in the screen
    cx, cy = (ix0 + ix1) / 2, (iy0 + iy1) / 2
    r = 58 * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=IRIS)
    r2 = 26 * s
    d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), fill=PUPIL)
    hr = 10 * s
    d.ellipse((cx - r2 - 2 * s - hr, cy - r2 - 2 * s - hr,
               cx - r2 - 2 * s + hr, cy - r2 - 2 * s + hr), fill=WHITE)
    # connection "signal" arcs to the right of the lens
    for i, rr in enumerate((84, 108)):
        bbox = (cx - rr * s, cy - rr * s, cx + rr * s, cy + rr * s)
        d.arc(bbox, start=-32, end=32, fill=IRIS, width=int(9 * s))
    return img


def main() -> None:
    master = render(512)
    (ASSETS).mkdir(exist_ok=True)
    for size in (16, 32, 48, 64, 128, 256, 512):
        icon = render(size) if size >= 64 else master.resize((size, size), Image.LANCZOS)
        icon.save(OUT / f"logo-{size}.png")
    master.save(OUT / "logo.png")
    master.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")
    # Windows .ico with multiple sizes for the .exe
    master.save(ASSETS / "teamlooker.ico", sizes=[(16, 16), (32, 32), (48, 48),
                                                  (64, 64), (128, 128), (256, 256)])
    master.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
