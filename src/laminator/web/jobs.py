"""Background jobs for the UI: one at a time (the same lock the CLI uses), with a log the page polls. A request that arrives while
another job is working waits in a queue and starts by itself, in the order it was made."""
from __future__ import annotations

import secrets
import threading
from collections import deque
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .. import actions
from ..mdblist import MDBListError
from ..actions import Event


@dataclass
class Job:
    id: str
    title: str
    status: str = "running"  # queued | running | done | failed | cancelled
    events: list[Event] = field(default_factory=list)
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    priority: bool = False       # a small request (one title): goes ahead of the queue, and may run in a gap of a big run
    interruptible: bool = False  # a big poster run that can pause between two titles to let a priority request through

    @property
    def problems(self) -> int:
        return sum(e.problem for e in self.events) + (1 if self.error else 0)


class JobManager:
    def __init__(self, data: Path, keep: int = 25, on_done: Callable[[], None] | None = None):
        self.data, self.keep, self.on_done = data, keep, on_done
        self.jobs: list[Job] = []
        self._pending: deque = deque()  # (job, work) in the order asked
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def running(self) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.status == "running"), None)

    def queued(self) -> list[Job]:
        with self._lock:
            return [j for j, _ in self._pending]

    def position(self, job: Job) -> int:
        """1 = next to start; 0 when the job is not waiting."""
        with self._lock:
            for n, (j, _) in enumerate(self._pending, 1):
                if j is job:
                    return n
        return 0

    def _insert(self, job: Job, work) -> None:
        """Queue order: priority requests first (in the order asked), then the rest. Caller holds the lock."""
        if not job.priority:
            self._pending.append((job, work))
            return
        n = sum(1 for j, _ in self._pending if j.priority)
        self._pending.insert(n, (job, work))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def recent(self) -> list[Job]:
        with self._lock:
            return list(reversed(self.jobs))

    def snapshot(self, job: Job) -> tuple[str, list[Event], str | None]:
        with self._lock:
            return job.status, list(job.events), job.error

    def start(self, title: str, work: Callable[[], Iterator[Event]], wait: bool = False, queue: bool = True,
              priority: bool = False, interruptible: bool = False) -> Job:
        """Run `work` in a thread, or queue it behind the job that is working. With queue=False (scheduled runs, which must not
        pile up) raises actions.Busy instead. Asking twice for the same thing while it waits returns the job already waiting."""
        with self._lock:
            busy = any(j.status in ("running", "queued") for j in self.jobs)
            if busy and not queue:
                raise actions.Busy("another job is still running")
            if busy:
                same = next((j for j, _ in self._pending if j.title == title), None)
                if same is not None:
                    return same
            job = Job(secrets.token_urlsafe(6), title, status="queued" if busy else "running", priority=priority, interruptible=interruptible)
            self.jobs.append(job)
            if busy:
                self._insert(job, work)
            while len(self.jobs) > self.keep:  # forget the oldest finished run (never one that is waiting or working)
                old = next((j for j in self.jobs if j.status not in ("queued", "running")), None)
                if old is None:
                    break
                self.jobs.remove(old)
            if not busy:  # nothing is working: this one starts at once, on a worker that then carries on with the queue
                self._worker = threading.Thread(target=self._drain, args=(job, work), daemon=True)
                self._worker.start()
        if wait:
            job.done.wait()
        return job

    def cancel(self, job_id: str) -> bool:
        """Take a run that is still waiting out of the queue. A run that has started cannot be stopped."""
        with self._lock:
            for n, (j, w) in enumerate(self._pending):
                if j.id == job_id and j.status == "queued":
                    del self._pending[n]
                    j.status, j.finished = "cancelled", time.time()
                    j.done.set()
                    return True
        return False

    def _drain(self, job: Job, work) -> None:
        while True:
            self._run(job, work)
            with self._lock:
                if not self._pending:
                    if self._worker is threading.current_thread():
                        self._worker = None
                    return
                job, work = self._pending.popleft()
                job.status, job.started = "running", time.time()

    def _next_priority(self):
        with self._lock:
            for n, (j, w) in enumerate(self._pending):
                if j.priority and j.status == "queued":
                    del self._pending[n]
                    j.status, j.started = "running", time.time()
                    return j, w
        return None

    def _finish(self, job: Job) -> None:
        with self._lock:
            job.status = "failed" if job.error or any(x.kind == "error" for x in job.events) else "done"
            job.finished = time.time()
        job.done.set()
        if self.on_done:
            self.on_done()

    def _steps(self, job: Job, work) -> None:
        """Run a job's work to the end, recording its lines. Errors become the job's error, never an exception."""
        try:
            for ev in work():
                with self._lock:
                    job.events.append(ev)
                if job.interruptible:  # between two titles: let a small request through, then carry on
                    while (nxt := self._next_priority()) is not None:
                        small, small_work = nxt
                        with self._lock:
                            job.events.append(Event("info", message=f"paused for a quick change: {small.title}"))
                        self._steps(small, small_work)
                        self._finish(small)
                        with self._lock:
                            job.events.append(Event("info", message="continuing"))
        except (actions.Busy, actions.SetupError, MDBListError) as e:
            job.error = str(e)
        except Exception as e:  # shown in the log; never crash the server
            job.error = f"{type(e).__name__}: {e}"

    def _run(self, job: Job, work) -> None:
        try:
            with actions.run_lock(self.data):  # excludes a CLI run in another process too
                self._steps(job, work)
        except actions.Busy as e:  # a command-line run in another process holds the lock
            job.error = str(e)
        finally:
            self._finish(job)
