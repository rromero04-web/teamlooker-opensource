from PIL import Image, ImageDraw

from teamlooker.inputctl import map_key
from teamlooker.screen import TileEncoder, apply_frame


def _image(box_x):
    img = Image.new("RGB", (300, 170), (20, 20, 20))
    ImageDraw.Draw(img).rectangle((box_x, 70, box_x + 20, 90), fill=(255, 0, 0))
    return img


def _close(a, b, tolerance=6):
    """JPEG is lossy: compare the mean per-channel error."""
    pa, pb = a.tobytes(), b.tobytes()
    return sum(abs(p - q) for p, q in zip(pa, pb)) / len(pa) <= tolerance


def test_first_frame_is_full_then_only_dirty_tiles():
    enc = TileEncoder(quality=90)
    meta, blob = enc.encode(_image(10))
    assert meta["rects"] == [[0, 0, 300, 170, len(blob)]]
    canvas = apply_frame(None, meta, blob)
    assert enc.encode(_image(10)) is None  # nothing changed

    meta, blob = enc.encode(_image(200))
    assert len(meta["rects"]) >= 1
    covered = sum(w * h for _, _, w, h, _ in meta["rects"])
    assert covered < 300 * 170 / 2
    for x, y, w, h, _ in meta["rects"]:
        assert x % 64 == 0 and y % 64 == 0 and x + w <= 300 and y + h <= 170
    canvas = apply_frame(canvas, meta, blob)
    assert _close(canvas, _image(200))


def test_scale_and_reset():
    enc = TileEncoder()
    enc.configure(scale=0.5)
    meta, _ = enc.encode(_image(10))
    assert (meta["width"], meta["height"]) == (150, 85)
    enc.reset()
    meta, _ = enc.encode(_image(10))
    assert len(meta["rects"]) == 1


def test_map_key():
    assert map_key("a", "KeyA") == ("char", "a")
    assert map_key("Enter", "Enter") == ("special", "enter")
    assert map_key("Shift", "ShiftRight") == ("special", "shift_r")
    assert map_key("F5", "F5") == ("special", "f5")
    assert map_key(" ", "Space") == ("special", "space")
    assert map_key("Unidentified", "") is None
