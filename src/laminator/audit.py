"""A small security log: sign-ins (good and bad), two-factor changes, password and key changes, sessions ended. One JSON object per line
in <data>/audit.log (mode 600), rotated at 1 MB (one old file kept). It never contains a password, key, code or session token."""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

from .iosafe import Flusher

FILE = "audit.log"
MAX_BYTES = 1_000_000


_queues: dict[Path, tuple[collections.deque, Flusher]] = {}
_guard = threading.Lock()


def _append(f: Path, lines: list[str]) -> None:
    """Writes already-formatted lines (runs on the flusher thread)."""
    if f.exists() and f.stat().st_size > MAX_BYTES:
        f.replace(f.with_name(FILE + ".1"))
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write("".join(lines))


def _queue(data: Path) -> tuple[collections.deque, Flusher]:
    with _guard:
        if data not in _queues:
            q: collections.deque = collections.deque(maxlen=1000)  # a stuck disk cannot grow this without bound

            def work(q=q, f=data / FILE) -> None:
                lines = []
                while q:
                    lines.append(q.popleft())
                if lines:
                    _append(f, lines)

            _queues[data] = (q, Flusher(work, "laminator-audit"))
        return _queues[data]


def log(data: Path, event: str, **fields) -> None:
    """Never blocks a request for more than a moment: the line is queued and a background thread writes it (see iosafe)."""
    try:
        line = json.dumps({"t": round(time.time(), 1), "event": event, **{k: str(v)[:200] for k, v in fields.items()}}) + "\n"
    except (TypeError, ValueError):
        return
    q, flusher = _queue(data)
    q.append(line)
    flusher.kick()  # waits briefly on a healthy disk, so the line is on disk when this returns; a full disk is ignored (see Flusher)


def tail(data: Path, n: int = 30) -> list[dict]:
    out = []
    for name in (FILE + ".1", FILE):
        try:
            for ln in (data / name).read_text().splitlines():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        except FileNotFoundError:
            continue
    return out[-n:][::-1]
