"""Runs the schedule inside `laminator serve`. Scheduled runs are ordinary jobs: they appear on the Runs page,
use the same run lock (so they never overlap a manual or CLI run) and are skipped, not queued, when busy.
Every configured server is scheduled on its own, from its own last-run record."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .. import actions, schedule


class Scheduler(threading.Thread):
    def __init__(self, sched: schedule.Schedule, targets: Callable[[], dict[str, Path]],
                 start: Callable[[str | None, str], object], tick_seconds: int = 30, root: Path | None = None,
                 paused: Callable[[], bool] | None = None):
        """targets() -> {server kind: that server's folder}; start(kind, 'changed'|'maintenance') starts a job;
        with `root`, a due scheduled backup is started as start(None, 'backup') (it belongs to no one server)."""
        super().__init__(daemon=True, name="laminator-scheduler")
        self.sched, self.targets, self.start_run, self.tick_seconds, self.root = sched, targets, start, tick_seconds, root
        self.paused = paused  # True while the data folder is not responding: nothing is started then
        self.stop_event = threading.Event()
        self.last_error: str | None = None

    def tick(self, now: float | None = None) -> str | None:
        """One decision. Returns what was started ('maintenance'/'changed'), 'baseline', 'busy' or None.
        At most one job starts per tick, so two servers never run at the same time."""
        result = None
        if self.paused is not None and self.paused():
            return "storage"
        if self.root is not None and schedule.backup_due(self.root, self.sched, now or time.time()):
            try:
                self.start_run(None, "backup")
            except actions.Busy:
                return "busy"  # not started, so no attempt is recorded: it is simply tried again next tick
            schedule.mark(self.root, "backup_try", now=now)  # a backup that then fails is retried hourly, not every tick
            return "backup"
        for kind, folder in self.targets().items():
            what = schedule.decide(folder, self.sched, now)
            if what in ("maintenance", "changed"):
                try:
                    self.start_run(kind, what)
                except actions.Busy:
                    return "busy"  # try again on the next tick
                return what
            result = result or what
        return result

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as e:  # a bad tick must never kill the thread
                self.last_error = f"{type(e).__name__}: {e}"
            self.stop_event.wait(self.tick_seconds)
