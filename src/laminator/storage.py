"""Watches the data folder. A probe reads from it every INTERVAL seconds (stat, list, read a small file). If one probe does not come back
within STALL_AFTER seconds the folder counts as not responding until it does: the web interface then answers "not responding" at once
instead of hanging, the health check fails, and scheduled runs wait. Only one probe is ever in flight, so a stuck disk cannot pile up
threads. Reads only: this must never add write load to a filesystem that may already be struggling. After the first probe the reads are served
from the filesystem's cache, so there is no physical disk I/O and no wear; the flip side is that a cache-served probe catches a stall in
the filesystem's own locks (the kind that hung this program) but not every kind of dead device."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

def _seconds(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, ""))
        return v if v >= 1 else default
    except ValueError:
        return default


INTERVAL = _seconds("LAMINATOR_STORAGE_PROBE_SECONDS", 15.0)   # how often the folder is probed
STALL_AFTER = _seconds("LAMINATOR_STORAGE_STALL_SECONDS", 20.0)  # a probe that has not come back after this long means "not responding"


def probe(data: Path) -> None:
    os.stat(data)
    with os.scandir(data) as it:
        next(it, None)
    for name in ("settings.yaml", "schedule.json"):
        try:
            with open(data / name, "rb") as fh:
                fh.read(64)
            break
        except FileNotFoundError:
            continue


class Watch:
    def __init__(self, data: Path):
        self.data = data
        self.probing_since: float | None = None
        self.stalled_since: float | None = None
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Watch":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="laminator-storage-watch", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.probing_since = time.monotonic()
            try:
                probe(self.data)
            except OSError:
                pass  # a missing or unreadable file is not a stall; only a probe that never returns is
            self.probing_since = None
            self.stop_event.wait(INTERVAL)

    @property
    def stalled(self) -> bool:
        b = self.probing_since
        return b is not None and time.monotonic() - b > STALL_AFTER

    @property
    def seconds(self) -> int:
        b = self.probing_since
        return int(time.monotonic() - b) if b is not None else 0
