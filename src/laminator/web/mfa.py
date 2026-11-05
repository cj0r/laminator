"""Two-factor sign-in with a time-based one-time code (TOTP, RFC 6238: 6 digits, 30 seconds, SHA-1, as every authenticator app uses)
and single-use recovery codes. Standard library only (the QR code is drawn by segno). Stored in <data>/mfa.json, mode 600:
the TOTP secret (it has to be readable to check codes; anyone who can read the data folder already holds everything else) and only
hashes of the recovery codes. A code that was used once cannot be used again (replay protection)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

FILE = "mfa.json"
STEP, DIGITS, WINDOW = 30, 6, 1  # accept the previous and next 30-second code too (clock drift)
N_RECOVERY = 10


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return str(n).zfill(DIGITS)


def match(secret: str, code: str, now: float, last_step: int = -1) -> int | None:
    """The time step a code belongs to, or None. A step at or before last_step is refused (each code works once)."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    t = int(now // STEP)
    for step in range(t - WINDOW, t + WINDOW + 1):
        if step > last_step and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str = "admin", issuer: str = "laminator") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_data_uri(text: str) -> str:
    """The QR code as an SVG data: URI (the page's Content-Security-Policy allows data: images, nothing else)."""
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0b0714", light="#ffffff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def _rc_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").replace(" ", "").encode()).hexdigest()


def _new_recovery() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    out = []
    for _ in range(N_RECOVERY):
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        out.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return out


# ---- the stored state --------------------------------------------------------------------------------------------------
def load(data: Path) -> dict:
    try:
        d = json.loads((data / FILE).read_text())
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: Path, d: dict) -> None:
    tmp = data / (FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh)
    tmp.replace(data / FILE)


def enabled(data: Path) -> bool:
    d = load(data)
    return bool(d.get("enabled") and d.get("secret"))


def recovery_left(data: Path) -> int:
    return len(load(data).get("recovery") or [])


def start(data: Path) -> str:
    """Begin setting up: a new secret waits until a code from the app proves it was scanned. An active setup is not touched."""
    d = load(data)
    d["pending"] = new_secret()
    d["pending_at"] = time.time()
    _save(data, d)
    return d["pending"]


def pending(data: Path) -> str | None:
    d = load(data)
    if d.get("pending") and time.time() - d.get("pending_at", 0) < 3600:
        return d["pending"]
    return None


def confirm(data: Path, code: str, now: float | None = None) -> list[str] | None:
    """Turn it on if the code matches the pending secret. Returns the recovery codes (shown once), or None."""
    now = now or time.time()
    d = load(data)
    sec = d.get("pending") if now - d.get("pending_at", 0) < 3600 else None
    if not sec:
        return None
    step = match(sec, code, now)
    if step is None:
        return None
    codes = _new_recovery()
    _save(data, {"enabled": True, "secret": sec, "last_step": step, "since": now, "recovery": [_rc_hash(c) for c in codes]})
    return codes


def verify(data: Path, code: str, now: float | None = None) -> str | None:
    """'code' or 'recovery' when the code is good (and it is used up), else None."""
    now = now or time.time()
    d = load(data)
    if not (d.get("enabled") and d.get("secret")):
        return None
    step = match(d["secret"], code, now, int(d.get("last_step", -1)))
    if step is not None:
        d["last_step"] = step
        _save(data, d)
        return "code"
    h = _rc_hash(code)
    left = d.get("recovery") or []
    for i, stored in enumerate(left):
        if len(code.strip()) >= 12 and hmac.compare_digest(stored, h):
            d["recovery"] = left[:i] + left[i + 1:]
            _save(data, d)
            return "recovery"
    return None


def new_recovery_codes(data: Path) -> list[str]:
    d = load(data)
    codes = _new_recovery()
    d["recovery"] = [_rc_hash(c) for c in codes]
    _save(data, d)
    return codes


def disable(data: Path) -> bool:
    try:
        (data / FILE).unlink()
        return True
    except FileNotFoundError:
        return False
