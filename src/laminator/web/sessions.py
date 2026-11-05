"""Server-side sessions. The browser holds a random token; the server keeps only its SHA-256, so a copy of the sessions file can't be used
to sign in. A session ends on logout (really: it is deleted here), after IDLE seconds without use, or ABSOLUTE seconds after sign-in,
whichever comes first. A password or two-factor change ends every other session."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from ..iosafe import Flusher

FILE = "sessions.json"
ABSOLUTE = 12 * 3600
IDLE = 2 * 3600
TOUCH_EVERY = 60  # how often last-seen is written back (seconds): no disk write on every request
MAX_SESSIONS = 50  # the oldest are dropped beyond this (a script logging in over and over cannot grow the file without bound)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, data: Path):
        self.path = data / FILE
        self.lock = threading.Lock()  # guards the in-memory table only; never held while the file is written
        self.items: dict[str, dict] = self._load()
        self.flusher = Flusher(self._write, "laminator-sessions")
        self.dirty = False

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
            return {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("created"), (int, float))}
        except (FileNotFoundError, ValueError, AttributeError):
            return {}

    def _write(self) -> None:
        """Runs on the flusher thread: take a snapshot under the lock (memory only), then write it with no lock held."""
        with self.lock:
            text = json.dumps(self.items)
        tmp = self.path.with_name(FILE + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        tmp.replace(self.path)

    def _save(self) -> None:
        """Ask for the table to be saved. Called with the lock held, so it must not wait: callers call flush() after releasing it."""
        self.dirty = True

    def flush(self) -> None:
        """Save if anything changed. Waits briefly on a healthy disk, and not at all while the disk is stuck: the in-memory table
        is what sign-in checks use, so a hung disk can never lock anyone out."""
        if self.dirty:
            self.dirty = False
            self.flusher.kick()

    def _prune(self, now: float) -> None:
        self.items = {k: v for k, v in self.items.items() if now - v["created"] < ABSOLUTE and now - v.get("seen", v["created"]) < IDLE}
        if len(self.items) > MAX_SESSIONS:
            keep = sorted(self.items.items(), key=lambda kv: kv[1].get("seen", 0), reverse=True)[:MAX_SESSIONS]
            self.items = dict(keep)

    def create(self, ip: str = "", agent: str = "", now: float | None = None) -> str:
        now = now or time.time()
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._prune(now)
            self.items[_h(token)] = {"created": now, "seen": now, "ip": ip[:64], "agent": agent[:120]}
            self._save()
        self.flush()
        return token

    def check(self, token: str | None, now: float | None = None) -> bool:
        if not token or len(token) > 200:
            return False
        now = now or time.time()
        with self.lock:
            s = self.items.get(_h(token))
            if s is None:
                return False
            if now - s["created"] >= ABSOLUTE or now - s.get("seen", s["created"]) >= IDLE:
                self.items.pop(_h(token), None)
                self._save()
                ok = False
            else:
                ok = True
                if now - s.get("seen", 0) >= TOUCH_EVERY:
                    s["seen"] = now
                    self._save()
        self.flush()
        return ok

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            if self.items.pop(_h(token), None) is not None:
                self._save()
        self.flush()

    def delete_others(self, keep: str | None) -> int:
        """End every session except `keep` (None: end them all). Returns how many ended."""
        with self.lock:
            k = _h(keep) if keep else None
            n = len([x for x in self.items if x != k])
            self.items = {x: v for x, v in self.items.items() if x == k}
            self._save()
        self.flush()
        return n

    def active(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        with self.lock:
            self._prune(now)
            return sorted(({"created": v["created"], "seen": v.get("seen", v["created"]), "ip": v.get("ip", ""), "agent": v.get("agent", "")}
                           for v in self.items.values()), key=lambda v: v["seen"], reverse=True)



def clear(data: Path) -> None:
    """End every session (used by the command line when it resets the password or two-factor sign-in)."""
    (data / FILE).unlink(missing_ok=True)
