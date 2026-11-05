"""The web UI password when it is set from Settings: stored as a salted scrypt hash (never the password itself) in
<data>/admin.hash, mode 600. It takes precedence over LAMINATOR_ADMIN_PASSWORD; removing it falls back to that one."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

FILE = "admin.hash"
MIN_LEN, MAX_LEN = 10, 200
_N, _R, _P = 2**14, 8, 1


def policy_error(pw: str) -> str | None:
    if len(pw) < MIN_LEN:
        return f"use at least {MIN_LEN} characters"
    if len(pw) > MAX_LEN:
        return f"use at most {MAX_LEN} characters"
    return None


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify(pw: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def load(data: Path) -> str | None:
    try:
        return (data / FILE).read_text().strip() or None
    except FileNotFoundError:
        return None


def save(data: Path, pw: str) -> None:
    err = policy_error(pw)
    if err:
        raise ValueError(err)
    tmp = data / (FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(hash_password(pw) + "\n")
    tmp.replace(data / FILE)


def remove(data: Path) -> bool:
    try:
        (data / FILE).unlink()
        return True
    except FileNotFoundError:
        return False
