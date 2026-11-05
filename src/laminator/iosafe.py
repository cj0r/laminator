"""Disk writes that cannot hang the program.

A filesystem fault (seen on one server running OpenZFS 2.4.2, 2026-11-04) can leave a thread blocked inside a system call for hours,
and such a thread cannot be killed. If that thread is a web request, or holds a lock every request needs, the whole interface stops:
the sign-in page, the health check, everything. So the small writes that happen while a request is being served (sessions, the audit
log) go through a Flusher: one background thread does the write, the request waits a moment for it (so a healthy disk behaves exactly
as before) and then carries on without it. While the thread is stuck, further requests do not wait at all, and the data is saved when
the disk comes back."""
from __future__ import annotations

import threading
import time
from typing import Callable

WAIT = 1.0          # how long a request waits for its write on a healthy disk
STUCK_AFTER = 2.0   # a write running longer than this counts as stuck: nobody waits for it any more


class Flusher:
    def __init__(self, work: Callable[[], None], name: str = "flusher"):
        """work() writes whatever is pending (it must take its own snapshot of the data, and must not hold a lock while it writes)."""
        self.work = work
        self.cond = threading.Condition()
        self.requested = 0
        self.done = 0
        self.busy_since: float | None = None
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._started = False

    def _loop(self) -> None:
        while True:
            with self.cond:
                while self.requested == self.done:
                    self.cond.wait()
                target = self.requested
                self.busy_since = time.monotonic()
            try:
                self.work()
                self.last_error = None
            except OSError as e:  # a full or broken disk must not take the program down; the next request tries again
                self.last_error = f"{type(e).__name__}: {e}"
            with self.cond:
                self.busy_since = None
                self.done = target
                self.cond.notify_all()

    @property
    def stuck(self) -> bool:
        b = self.busy_since
        return b is not None and time.monotonic() - b > STUCK_AFTER

    def kick(self, wait: float | None = None) -> bool:
        """Ask for a write. True when it finished within `wait` seconds, False when the caller moved on without it."""
        wait = WAIT if wait is None else wait
        with self.cond:
            if not self._started:
                self._started = True
                self._thread.start()
            self.requested += 1
            mine = self.requested
            self.cond.notify_all()
            if self.stuck:
                return False
            deadline = time.monotonic() + wait
            while self.done < mine:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
            return True
