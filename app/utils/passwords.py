"""Password hashing for the emergency admin secret — scrypt, standard library only.

The secret is configured as a hash, never as plaintext. Hashes carry their
parameters, PHC-style, so the cost can be raised later without breaking existing
configuration — but with colons, not ``$``, as separators::

    scrypt:ln=14,r=8,p=1:<salt, base64 without padding>:<digest, base64 without padding>

The value lives in ``.env``, and ``$`` there is a variable reference to Docker
Compose (``$scrypt`` expands to nothing) and to some shells; a mangled hash would
refuse the operator at the worst moment. Nothing in this encoding is special to
dotenv, Compose interpolation, YAML or a shell.

Verification is constant-time on the digest (:func:`hmac.compare_digest`).
scrypt is deliberately slow and memory-hard, so callers on the event loop run
:func:`verify_password` in a worker thread. Nothing here logs, and no function
puts the password or the digest into an exception message.

Make a hash for ``.env`` with ``python -m app.hash_emergency_password``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass

SCHEME = "scrypt"
DEFAULT_LOG_N = 14  # 2**14 iterations, 16 MiB — well under 100 ms on a small VPS
DEFAULT_R = 8
DEFAULT_P = 1
SALT_LENGTH = 16
KEY_LENGTH = 32
# Bounds on what a configured hash may ask of the process: a hostile or mistyped
# parameter set must not be able to exhaust memory or stall the bot.
MIN_LOG_N, MAX_LOG_N = 10, 17
MAX_R, MAX_P = 32, 16
MEMORY_LIMIT = 128 * 1024 * 1024

_ENCODED = re.compile(
    r"^scrypt:ln=(?P<ln>\d{1,2}),r=(?P<r>\d{1,3}),p=(?P<p>\d{1,3})"
    r":(?P<salt>[A-Za-z0-9+/]+):(?P<digest>[A-Za-z0-9+/]+)$"
)


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("password hash carries invalid base64") from exc


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """A parsed hash: its cost parameters, salt and digest."""

    log_n: int
    r: int
    p: int
    salt: bytes
    digest: bytes

    def __post_init__(self) -> None:
        if not MIN_LOG_N <= self.log_n <= MAX_LOG_N:
            raise ValueError(f"password hash ln must be between {MIN_LOG_N} and {MAX_LOG_N}")
        if not 1 <= self.r <= MAX_R or not 1 <= self.p <= MAX_P:
            raise ValueError(f"password hash r must be 1..{MAX_R} and p 1..{MAX_P}")
        if self.memory > MEMORY_LIMIT:
            raise ValueError("password hash parameters need more memory than allowed")
        if len(self.salt) < 8 or len(self.digest) < 16:
            raise ValueError("password hash salt or digest is too short")

    @property
    def n(self) -> int:
        return 1 << self.log_n

    @property
    def memory(self) -> int:
        """Bytes scrypt needs for these parameters (OpenSSL's own estimate)."""
        return 128 * self.r * (self.n + self.p + 2)

    def encode(self) -> str:
        return (
            f"{SCHEME}:ln={self.log_n},r={self.r},p={self.p}"
            f":{_b64encode(self.salt)}:{_b64encode(self.digest)}"
        )

    def derive(self, password: str) -> bytes:
        """The digest ``password`` produces under this hash's salt and parameters."""
        return hashlib.scrypt(
            _normalize(password),
            salt=self.salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=len(self.digest),
            maxmem=MEMORY_LIMIT + 1024 * 1024,
        )

    def __repr__(self) -> str:  # never the salt or the digest
        return f"PasswordHash({SCHEME}, ln={self.log_n}, r={self.r}, p={self.p})"


def _normalize(password: str) -> bytes:
    """One byte sequence per password, however a client composed its characters."""
    return unicodedata.normalize("NFC", password).encode("utf-8")


def parse_password_hash(encoded: str) -> PasswordHash:
    """Parse an encoded hash. Raises :class:`ValueError` (never echoing the input)."""
    if not isinstance(encoded, str):
        raise ValueError("password hash must be a string")
    match = _ENCODED.match(encoded.strip())
    if match is None:
        raise ValueError(
            "password hash is not in the expected format "
            "(scrypt:ln=…,r=…,p=…:salt:digest — see python -m app.hash_emergency_password)"
        )
    return PasswordHash(
        log_n=int(match["ln"]),
        r=int(match["r"]),
        p=int(match["p"]),
        salt=_b64decode(match["salt"]),
        digest=_b64decode(match["digest"]),
    )


def hash_password(
    password: str,
    *,
    log_n: int = DEFAULT_LOG_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
) -> str:
    """Hash ``password`` with a fresh random salt. Refuses an empty password."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(SALT_LENGTH)
    probe = PasswordHash(log_n=log_n, r=r, p=p, salt=salt, digest=bytes(KEY_LENGTH))
    return PasswordHash(log_n=log_n, r=r, p=p, salt=salt, digest=probe.derive(password)).encode()


def verify_password(password: str, encoded: str) -> bool:
    """
    Whether ``password`` matches ``encoded``. Blocking: run it off the event loop.

    Fails closed: an empty password or a hash that does not parse is ``False``,
    never an exception a caller might answer with details.
    """
    if not isinstance(password, str) or not password:
        return False
    try:
        parsed = parse_password_hash(encoded)
    except ValueError:
        return False
    return hmac.compare_digest(parsed.derive(password), parsed.digest)
