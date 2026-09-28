"""Persistent per-user settings (device identity, permanent password, history)."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from teamlooker import DEFAULT_RELAY_PORT
from teamlooker.srp import create_verifier

DEFAULT_PERMISSIONS = {"control": True, "files": True, "clipboard": True}
MAX_RECENT = 12


def config_dir() -> Path:
    override = os.environ.get("TEAMLOOKER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "TeamLooker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TeamLooker"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "teamlooker"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "TeamLooker"


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.json"
        self._lock = threading.Lock()
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except ValueError:
                self.data = {}
        changed = False
        if "identity_key" not in self.data:
            raw = Ed25519PrivateKey.generate().private_bytes(
                Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            self.data["identity_key"] = base64.b64encode(raw).decode()
            changed = True
        self.data.setdefault("relay", os.environ.get("TEAMLOOKER_RELAY",
                                                     f"127.0.0.1:{DEFAULT_RELAY_PORT}"))
        self.data.setdefault("device_name", _hostname())
        perms = dict(DEFAULT_PERMISSIONS)
        perms.update(self.data.get("permissions", {}))
        self.data["permissions"] = perms
        self.data.setdefault("recent", [])
        self.data.setdefault("permanent_password", None)
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)

    @property
    def identity_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.data["identity_key"]))

    @property
    def relay(self) -> str:
        return self.data["relay"]

    @relay.setter
    def relay(self, value: str) -> None:
        self.data["relay"] = value
        self.save()

    @property
    def permissions(self) -> dict:
        return dict(self.data["permissions"])

    def set_permissions(self, **changes: bool) -> None:
        for key, value in changes.items():
            if key in DEFAULT_PERMISSIONS:
                self.data["permissions"][key] = bool(value)
        self.save()

    def set_permanent_password(self, password: str | None) -> None:
        """Store only an SRP verifier, never the password itself."""
        if password:
            salt, verifier = create_verifier(password)
            self.data["permanent_password"] = {"salt": salt.hex(), "verifier": verifier.hex()}
        else:
            self.data["permanent_password"] = None
        self.save()

    @property
    def permanent_verifier(self) -> tuple[bytes, bytes] | None:
        entry = self.data.get("permanent_password")
        if not entry:
            return None
        return bytes.fromhex(entry["salt"]), bytes.fromhex(entry["verifier"])

    @property
    def recent(self) -> list[dict]:
        return list(self.data["recent"])

    def add_recent(self, partner_id: str, name: str = "") -> None:
        recent = [r for r in self.data["recent"] if r.get("id") != partner_id]
        previous = next((r for r in self.data["recent"] if r.get("id") == partner_id), {})
        recent.insert(0, {"id": partner_id, "name": name or previous.get("name", ""),
                          "last": int(time.time())})
        self.data["recent"] = recent[:MAX_RECENT]
        self.save()

    def remove_recent(self, partner_id: str) -> None:
        self.data["recent"] = [r for r in self.data["recent"] if r.get("id") != partner_id]
        self.save()


def _hostname() -> str:
    import socket
    return socket.gethostname()
