"""Remote input injection (mouse and keyboard) via ``pynput``.

Viewers send browser-style key identifiers (``KeyboardEvent.key``/``code``);
:func:`map_key` turns them into a portable description that the controller
resolves to ``pynput`` keys. Keeping the mapping pure makes it testable on
machines without a display.
"""

from __future__ import annotations

import logging

log = logging.getLogger("teamlooker.input")

SPECIAL_KEYS = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc",
    " ": "space", "Spacebar": "space",
    "ArrowLeft": "left", "ArrowRight": "right", "ArrowUp": "up", "ArrowDown": "down",
    "Shift": "shift", "Control": "ctrl", "Alt": "alt", "AltGraph": "alt_gr",
    "Meta": "cmd", "OS": "cmd", "CapsLock": "caps_lock", "Delete": "delete",
    "Home": "home", "End": "end", "PageUp": "page_up", "PageDown": "page_down",
    "Insert": "insert", "ContextMenu": "menu", "NumLock": "num_lock",
    "PrintScreen": "print_screen", "ScrollLock": "scroll_lock", "Pause": "pause",
    "AudioVolumeMute": "media_volume_mute", "AudioVolumeDown": "media_volume_down",
    "AudioVolumeUp": "media_volume_up", "MediaPlayPause": "media_play_pause",
    "MediaTrackNext": "media_next", "MediaTrackPrevious": "media_previous",
}
RIGHT_MODIFIERS = {"ShiftRight": "shift_r", "ControlRight": "ctrl_r",
                   "AltRight": "alt_r", "MetaRight": "cmd_r"}
MOUSE_BUTTONS = {0: "left", 1: "middle", 2: "right"}


def map_key(key: str, code: str = "") -> tuple[str, str] | None:
    """Return ``("special", name)``, ``("char", c)`` or ``None`` if unmappable."""
    if code in RIGHT_MODIFIERS:
        return "special", RIGHT_MODIFIERS[code]
    if key in SPECIAL_KEYS:
        return "special", SPECIAL_KEYS[key]
    if len(key) > 1 and key[0] == "F" and key[1:].isdigit() and 1 <= int(key[1:]) <= 20:
        return "special", key.lower()
    if len(key) == 1:
        return "char", key
    return None


class InputController:
    def __init__(self):
        self._mouse = None
        self._keyboard = None
        self._pressed: dict[str, object] = {}
        self._buttons: set[str] = set()
        self.error: str | None = None
        try:
            from pynput import keyboard, mouse
            self._mouse = mouse.Controller()
            self._keyboard = keyboard.Controller()
            self._kb = keyboard
            self._ms = mouse
        except Exception as exc:  # no display, missing backend, ...
            self.error = str(exc)
            log.warning("remote input unavailable: %s", exc)

    @property
    def available(self) -> bool:
        return self._mouse is not None

    def position(self) -> tuple[int, int] | None:
        if not self._mouse:
            return None
        try:
            x, y = self._mouse.position
            return int(x), int(y)
        except Exception:
            return None

    def move(self, x: int, y: int) -> None:
        if self._mouse:
            self._mouse.position = (int(x), int(y))

    def button(self, button: int, down: bool) -> None:
        name = MOUSE_BUTTONS.get(button)
        if not self._mouse or name is None:
            return
        btn = getattr(self._ms.Button, name)
        if down:
            self._mouse.press(btn)
            self._buttons.add(name)
        else:
            self._mouse.release(btn)
            self._buttons.discard(name)

    def scroll(self, dx: int, dy: int) -> None:
        if self._mouse:
            self._mouse.scroll(int(dx), int(dy))

    def _resolve(self, key: str, code: str):
        mapped = map_key(key, code)
        if mapped is None:
            return None
        kind, value = mapped
        if kind == "char":
            return self._kb.KeyCode.from_char(value)
        return getattr(self._kb.Key, value, None)

    def key(self, key: str, code: str, down: bool) -> None:
        if not self._keyboard:
            return
        ident = code or key
        try:
            if down:
                resolved = self._resolve(key, code)
                if resolved is None:
                    return
                self._keyboard.press(resolved)
                self._pressed[ident] = resolved
            else:
                # Release exactly what was pressed (the key text may differ
                # between keydown and keyup, e.g. "A" vs "a" around Shift).
                resolved = self._pressed.pop(ident, None) or self._resolve(key, code)
                if resolved is not None:
                    self._keyboard.release(resolved)
        except Exception as exc:
            log.debug("key injection failed for %r: %s", key, exc)

    def type_text(self, text: str) -> None:
        if self._keyboard:
            self._keyboard.type(text)

    def combo(self, names: list[str]) -> None:
        """Press and release a combination such as ``["ctrl", "alt", "delete"]``."""
        if not self._keyboard:
            return
        keys = [getattr(self._kb.Key, n, None) or self._kb.KeyCode.from_char(n) for n in names]
        for k in keys:
            self._keyboard.press(k)
        for k in reversed(keys):
            self._keyboard.release(k)

    def release_all(self) -> None:
        """Called when a session ends so no key or button stays stuck."""
        for resolved in list(self._pressed.values()):
            try:
                self._keyboard.release(resolved)
            except Exception:
                pass
        self._pressed.clear()
        for name in list(self._buttons):
            try:
                self._mouse.release(getattr(self._ms.Button, name))
            except Exception:
                pass
        self._buttons.clear()
