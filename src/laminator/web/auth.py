"""The interface password check, the signing key (CSRF tokens, the two-factor step) and the sign-in rate limit. Sessions themselves are in
sessions.py (server-side, revocable). sign()/valid() are kept for small signed values."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "laminator_session"
TTL = 12 * 3600
MAX_FAILS, WINDOW = 5, 300


def load_secret(data: Path) -> bytes:
    f = data / ".secret"
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from the first byte, never readable by others even briefly
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return f.read_bytes()


def rotate_secret(data: Path) -> bytes:
    """A new signing key: every existing session cookie stops being valid."""
    f = data / ".secret"
    tmp = data / ".secret.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secrets.token_bytes(32))
    tmp.replace(f)
    return f.read_bytes()


def sign(secret: bytes, expires: int) -> str:
    mac = hmac.new(secret, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def valid(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp, _, mac = token.partition(".")
    if not exp.isdigit():
        return False
    good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, good) and int(exp) > (now or time.time())


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(expected.encode()).digest())


class RateLimit:
    """Per-client failed-login limiter (in memory: resets on restart, which is fine for a LAN tool)."""

    def __init__(self):
        self.fails: dict[str, list[float]] = {}

    def blocked(self, who: str, now: float | None = None) -> bool:
        now = now or time.time()
        recent = [t for t in self.fails.get(who, []) if now - t < WINDOW]
        self.fails[who] = recent
        return len(recent) >= MAX_FAILS

    def fail(self, who: str, now: float | None = None) -> None:
        now = now or time.time()
        if len(self.fails) > 5000:  # many different addresses: forget the ones whose window has passed (memory stays bounded)
            self.fails = {k: v for k, v in self.fails.items() if v and now - v[-1] < WINDOW}
        self.fails.setdefault(who, []).append(now)

    def ok(self, who: str) -> None:
        self.fails.pop(who, None)
