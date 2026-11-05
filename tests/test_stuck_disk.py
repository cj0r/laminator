"""A stuck disk (a thread blocked inside the kernel, as on one server with OpenZFS 2.4.2, 2026-11-04) must not hang the interface.
Nothing here uses a real stuck disk: the write or the probe is replaced with a function that blocks until the test lets it go."""
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from laminator import audit, iosafe, storage
from laminator.store import Store
from laminator.web import auth, sessions
from laminator.web.app import create_app
from laminator.web.scheduler import Scheduler
from test_web import FakeServer

CFG = {"server": {"kind": "emby", "url": "http://x"}}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(iosafe, "WAIT", 0.2)
    monkeypatch.setattr(iosafe, "STUCK_AFTER", 0.3)


def took(fn):
    t = time.monotonic()
    out = fn()
    return out, time.monotonic() - t


def test_flusher_waits_on_a_healthy_disk_and_gives_up_on_a_stuck_one():
    gate, writes = threading.Event(), []
    stuck = threading.Event()

    def work():
        if stuck.is_set():
            gate.wait(10)
        writes.append(time.monotonic())

    f = iosafe.Flusher(work)
    assert f.kick() is True and len(writes) == 1                      # healthy: the caller really waited for the write
    stuck.set()
    ok, secs = took(f.kick)
    assert ok is False and secs < 1                                    # stuck: gave up after WAIT
    time.sleep(0.4)
    assert f.stuck
    ok, secs = took(f.kick)
    assert ok is False and secs < 0.1                                  # already known to be stuck: no waiting at all
    gate.set()
    stuck.clear()
    deadline = time.monotonic() + 3
    while f.busy_since is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not f.stuck and f.kick() is True                            # the disk came back: back to normal


def test_signing_in_and_checking_sessions_still_work_while_the_sessions_file_cannot_be_written(tmp_path):
    s = sessions.SessionStore(tmp_path)
    gate = threading.Event()
    real = s._write
    s._write = lambda: (gate.wait(10), real())                          # the disk hangs on every write
    tok, secs = took(s.create)
    assert secs < 1.5
    for _ in range(5):
        ok, secs = took(lambda: s.check(tok, now=time.time() + 120))    # each check also wants to save "last seen"
        assert ok and secs < 1.5
    gate.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (tmp_path / "sessions.json").exists():
        time.sleep(0.02)
    assert (tmp_path / "sessions.json").exists()                        # and what was waiting is saved once the disk answers


def test_audit_lines_are_kept_and_written_later_when_the_disk_is_stuck(tmp_path, monkeypatch):
    gate = threading.Event()
    real = audit._append
    monkeypatch.setattr(audit, "_append", lambda f, lines: (gate.wait(10), real(f, lines)))
    _, secs = took(lambda: audit.log(tmp_path, "wrong password", ip="203.0.113.9"))
    assert secs < 1.5
    _, secs = took(lambda: audit.log(tmp_path, "signed in", ip="203.0.113.9"))
    assert secs < 0.5
    gate.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and {"wrong password", "signed in"} - {e["event"] for e in audit.tail(tmp_path)}:
        time.sleep(0.02)                                                # the writer thread catches up in the background
    events = [e["event"] for e in audit.tail(tmp_path)]
    assert "wrong password" in events and "signed in" in events


def test_storage_watch_reports_a_probe_that_never_returns_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "STALL_AFTER", 0.2)
    monkeypatch.setattr(storage, "INTERVAL", 0.05)
    gate = threading.Event()
    calls = []
    real = storage.probe

    def blocked(d):                                                     # other tests' apps leave their own watchers running: only count ours
        if d == tmp_path:
            calls.append(1)
            gate.wait(10)
        else:
            real(d)
    monkeypatch.setattr(storage, "probe", blocked)
    w = storage.Watch(tmp_path).start()
    try:
        time.sleep(0.5)
        assert w.stalled and w.seconds >= 0
        time.sleep(0.3)
        assert len(calls) == 1                                          # one probe in flight, never a pile of them
        gate.set()
        time.sleep(0.3)
        assert not w.stalled
    finally:
        w.stop()


def test_the_probe_only_reads(tmp_path):
    (tmp_path / "settings.yaml").write_text("server: {}\n")
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    storage.probe(tmp_path)
    assert before == {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()} and len(list(tmp_path.iterdir())) == 1


def test_the_interface_answers_at_once_when_the_data_folder_does_not_respond(tmp_path, monkeypatch):
    app = create_app(dict(CFG), FakeServer(), tmp_path, "pw-long-enough")
    c = TestClient(app, follow_redirects=False, client=("198.51.100.7", 50000))
    assert c.get("/healthz").json() == {"ok": True}
    monkeypatch.setattr(storage, "STALL_AFTER", 0.0)
    app.state.storage.probing_since = time.monotonic() - 30            # a probe that has been running for 30 seconds
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["storage"] == "not responding"
    for path in ("/", "/login", "/library", "/settings"):
        r = c.get(path)
        assert r.status_code == 503 and "not responding" in r.text and r.headers["retry-after"] == "15", path
    assert c.get("/static/style.css").status_code == 200               # static files do not touch the data folder
    app.state.storage.probing_since = None
    assert c.get("/healthz").json() == {"ok": True} and c.get("/login").status_code == 200


def test_the_scheduler_starts_nothing_while_the_folder_does_not_respond(tmp_path):
    started = []
    sch = Scheduler(object(), lambda: {"emby": tmp_path}, lambda kind, what: started.append((kind, what)), root=tmp_path, paused=lambda: True)
    assert sch.tick() == "storage" and started == []


def test_state_database_left_in_wal_mode_is_copied_once_before_it_is_converted(tmp_path):
    db = tmp_path / "state.db"
    st = Store(db)
    st.db.execute("PRAGMA journal_mode=WAL")
    st.put_original("1", "Alpha", str(tmp_path / "a.img"), "sha", "tag")
    st.db.close()                                                       # the row may still be only in the -wal file of an older build
    st = Store(db)                                                      # converts, and keeps a copy first
    keep = tmp_path / "state.db.pre-persist"
    assert keep.exists()
    import sqlite3
    con = sqlite3.connect(keep)
    assert con.execute("select count(*) from posters").fetchone()[0] == 1
    con.close()
    first = keep.stat().st_mtime_ns
    st.put_original("2", "Beta", str(tmp_path / "b.img"), "sha", "tag")
    st.db.close()
    Store(db).db.close()
    assert keep.stat().st_mtime_ns == first                             # never overwritten
    assert not (tmp_path / "state.db.pre-persist.tmp").exists()


def test_a_database_that_was_never_wal_gets_no_copy(tmp_path):
    Store(tmp_path / "state.db").db.close()
    assert not (tmp_path / "state.db.pre-persist").exists()


def test_probe_timing_defaults_and_environment_overrides(monkeypatch):
    monkeypatch.delenv("LAMINATOR_STORAGE_PROBE_SECONDS", raising=False)
    assert storage._seconds("LAMINATOR_STORAGE_PROBE_SECONDS", 15.0) == 15.0
    monkeypatch.setenv("LAMINATOR_STORAGE_PROBE_SECONDS", "30")
    assert storage._seconds("LAMINATOR_STORAGE_PROBE_SECONDS", 15.0) == 30.0
    for bad in ("", "abc", "0", "-5", "0.5"):                                    # unusable values fall back to the default
        monkeypatch.setenv("LAMINATOR_STORAGE_PROBE_SECONDS", bad)
        assert storage._seconds("LAMINATOR_STORAGE_PROBE_SECONDS", 15.0) == 15.0, bad
