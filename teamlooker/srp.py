"""SRP-6a password-authenticated key exchange (RFC 5054, 2048-bit group, SHA-256).

The host only stores a verifier derived from the password, the password never
travels over the network, and a relay sitting in the middle cannot run an
offline dictionary attack against what it observes. Both sides end up with the
same session key ``K`` only when the viewer typed the right password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4"
    "A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0CF60"
    "95179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF"
    "747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B907"
    "8717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861"
    "60279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DB"
    "FBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
N = int(N_HEX, 16)
G = 2
N_BYTES = (N.bit_length() + 7) // 8
IDENTITY = b"teamlooker"


class SRPError(Exception):
    pass


def _h(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _pad(value: int) -> bytes:
    return value.to_bytes(N_BYTES, "big")


def _bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


K_MULT = _int(_h(_pad(N), _pad(G)))


def _private_key(salt: bytes, password: str) -> int:
    return _int(_h(salt, _h(IDENTITY + b":" + password.encode("utf-8"))))


def create_verifier(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """Return ``(salt, verifier)`` for storing a password on the host."""
    salt = salt or secrets.token_bytes(16)
    x = _private_key(salt, password)
    return salt, _pad(pow(G, x, N))


def _proof(a_pub: int, b_pub: int, salt: bytes, key: bytes) -> bytes:
    hn = _h(_pad(N))
    hg = _h(_pad(G))
    hxor = bytes(x ^ y for x, y in zip(hn, hg))
    return _h(hxor, _h(IDENTITY), salt, _pad(a_pub), _pad(b_pub), key)


class SRPServer:
    """Host side: knows the verifier, checks the viewer's proof."""

    def __init__(self, salt: bytes, verifier: bytes):
        self.salt = salt
        self._v = _int(verifier)
        self._b = _int(secrets.token_bytes(32))
        self.B = (K_MULT * self._v + pow(G, self._b, N)) % N
        self.session_key: bytes | None = None
        self._expected_m1: bytes | None = None
        self._a_pub: int | None = None

    def process_client(self, a_pub: int) -> None:
        if a_pub % N == 0:
            raise SRPError("invalid client public value")
        u = _int(_h(_pad(a_pub), _pad(self.B)))
        if u == 0:
            raise SRPError("invalid scrambling parameter")
        secret = pow(a_pub * pow(self._v, u, N), self._b, N)
        key = _h(_pad(secret))
        self._a_pub = a_pub
        self._expected_m1 = _proof(a_pub, self.B, self.salt, key)
        self.session_key = key

    def verify(self, m1: bytes) -> bytes | None:
        """Return the server proof M2 when M1 is valid, else ``None``."""
        if self._expected_m1 is None or self.session_key is None:
            raise SRPError("process_client() must be called first")
        if not hmac.compare_digest(m1, self._expected_m1):
            return None
        return _h(_pad(self._a_pub), m1, self.session_key)


class SRPClient:
    """Viewer side: knows the password.

    One client value ``A`` can answer several host challenges (a host may
    accept both its one-time session password and a permanent password).
    """

    def __init__(self, password: str):
        self._password = password
        self._a = _int(secrets.token_bytes(32))
        self.A = pow(G, self._a, N)

    def process_challenge(self, salt: bytes, b_pub: int) -> tuple[bytes, bytes]:
        """Return ``(M1, session_key)`` for the host's ``(salt, B)`` challenge."""
        if b_pub % N == 0:
            raise SRPError("invalid server public value")
        u = _int(_h(_pad(self.A), _pad(b_pub)))
        if u == 0:
            raise SRPError("invalid scrambling parameter")
        x = _private_key(salt, self._password)
        base = (b_pub - K_MULT * pow(G, x, N)) % N
        secret = pow(base, self._a + u * x, N)
        key = _h(_pad(secret))
        return _proof(self.A, b_pub, salt, key), key

    def verify_server(self, m1: bytes, key: bytes, m2: bytes) -> bool:
        return hmac.compare_digest(m2, _h(_pad(self.A), m1, key))


def int_to_hex(value: int) -> str:
    return _bytes(value).hex()


def hex_to_int(value: str) -> int:
    return int(value, 16)
