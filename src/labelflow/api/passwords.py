"""Password hashing with the standard library's scrypt.

A stored hash is ``scrypt$<log2 N>$<r>$<p>$<salt>$<hash>`` (salt and hash in base64), so the
cost can be raised later without breaking old hashes: each one says how it was made. The
salt is random per password and the comparison takes the same time however many characters
match.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

R = 8
P = 1
KEY_BYTES = 32
SALT_BYTES = 16


def _scrypt(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    n = 2**log_n
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=256 * n * r
    )


def hash_password(password: str, log_n: int = 14) -> str:
    """A new hash of ``password``, with a random salt."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, log_n, R, P)
    fields = ("scrypt", str(log_n), str(R), str(P), _b64(salt), _b64(key))
    return "$".join(fields)


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches a stored hash (False for a hash that cannot be read)."""
    try:
        kind, log_n, r, p, salt, key = stored.split("$")
        if kind != "scrypt":
            return False
        expected = base64.b64decode(key)
        actual = _scrypt(password, base64.b64decode(salt), int(log_n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
