"""Passwords (argon2), TOTP codes (RFC 6238), session tokens, and sealing small secrets at rest."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets as token_secrets
import struct
import time
from urllib.parse import quote

TOTP_STEP = 30
TOTP_DIGITS = 6
MIN_PASSWORD = 12


# ---------------------------------------------------------------- passwords
def _hasher():
    from argon2 import PasswordHasher

    return PasswordHasher()        # argon2id with the library's current defaults


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    """Constant work whether or not the user exists (``hashed`` None compares against a dummy)."""
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    try:
        return _hasher().verify(hashed or _dummy_hash(), password) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(token_secrets.token_urlsafe(16)))
    return _DUMMY[0]


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"mật khẩu cần ít nhất {MIN_PASSWORD} ký tự"
    if len(set(password)) < 6:
        return "mật khẩu quá đơn giản"
    return None


# ---------------------------------------------------------------- TOTP (RFC 6238, SHA-1, 30 s, 6 digits)
def new_totp_secret() -> str:
    return base64.b32encode(token_secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def totp_code(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_now(secret: str, now: float | None = None) -> str:
    return totp_code(secret, int((time.time() if now is None else now) // TOTP_STEP))


def verify_totp(secret: str, code: str, *, now: float, last_counter: int = 0) -> int | None:
    """The counter the code matches (one step of clock drift either way), or None. A code at or before
    ``last_counter`` was already used: refused, so a code seen over someone's shoulder is spent."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != TOTP_DIGITS:
        return None
    current = int(now // TOTP_STEP)
    for counter in (current, current - 1, current + 1):
        if counter > last_counter and hmac.compare_digest(totp_code(secret, counter), code):
            return counter
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "Kite") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")


# ---------------------------------------------------------------- tokens
def new_token() -> str:
    return token_secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- sealing (AES-GCM, key in a 0600 file)
class Sealer:
    """Encrypts small secrets kept in the database (the TOTP secret) with a key that never leaves the
    machine: a database copy alone does not give the second factor away."""

    def __init__(self, key_file: Path) -> None:
        self.key_file = Path(key_file)

    def _key(self) -> bytes:
        try:
            key = base64.b64decode(self.key_file.read_text(encoding="ascii").strip())
            if len(key) == 32:
                return key
        except (OSError, ValueError):
            pass
        if self.key_file.exists():
            raise RuntimeError(f"{self.key_file} is damaged; restore it from a backup")
        key = token_secrets.token_bytes(32)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.key_file.parent, 0o700)
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(base64.b64encode(key).decode("ascii") + "\n")
        return key

    def seal(self, text: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = token_secrets.token_bytes(12)
        return base64.b64encode(nonce + AESGCM(self._key()).encrypt(nonce, text.encode(), b"kite-web")).decode()

    def unseal(self, blob: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.b64decode(blob)
        return AESGCM(self._key()).decrypt(raw[:12], raw[12:], b"kite-web").decode()
