"""Text clipboard access via ``pyperclip`` (optional)."""

from __future__ import annotations

MAX_CLIPBOARD = 1024 * 1024


class Clipboard:
    def __init__(self):
        try:
            import pyperclip
            pyperclip.paste()
            self._pc = pyperclip
        except Exception:
            self._pc = None

    @property
    def available(self) -> bool:
        return self._pc is not None

    def get(self) -> str | None:
        if not self._pc:
            return None
        try:
            text = self._pc.paste()
        except Exception:
            return None
        return text if isinstance(text, str) and len(text) <= MAX_CLIPBOARD else None

    def set(self, text: str) -> None:
        if self._pc and len(text) <= MAX_CLIPBOARD:
            try:
                self._pc.copy(text)
            except Exception:
                pass
