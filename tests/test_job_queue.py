import threading

import pytest

from laminator import actions
from laminator.actions import Event
from laminator.web.jobs import JobManager


def gen(name, gate=None, n=3, log=None):
    def work():
        for i in range(n):
            if gate is not None and i == 0:
                gate.wait(5)
            if log is not None:
                log.append(f"{name}{i}")
            yield Event("apply", f"{name}{i}", "x")
    return work


def test_a_request_made_while_busy_waits_in_line_and_runs_by_itself(tmp_path):
    jm = JobManager(tmp_path)
    gate, order = threading.Event(), []
    a = jm.start("A", gen("a", gate, log=order))
    b = jm.start("B", gen("b", log=order))
    c = jm.start("C", gen("c", log=order))
    assert (a.status, b.status, c.status) == ("running", "queued", "queued") and jm.position(b) == 1 and jm.position(c) == 2
    gate.set()
    for j in (a, b, c):
        assert j.done.wait(5) and j.status == "done"
    assert order == ["a0", "a1", "a2", "b0", "b1", "b2", "c0", "c1", "c2"]             # strictly in the order asked


def test_asking_again_for_something_already_waiting_does_not_queue_it_twice(tmp_path):
    jm = JobManager(tmp_path)
    gate = threading.Event()
    jm.start("A", gen("a", gate))
    b1, b2 = jm.start("B", gen("b")), jm.start("B", gen("b"))
    assert b1 is b2 and len(jm.queued()) == 1
    gate.set(); b1.done.wait(5)


def test_a_small_request_runs_in_a_gap_of_a_big_run_which_then_carries_on(tmp_path):
    jm = JobManager(tmp_path)
    gate, order = threading.Event(), []
    big = jm.start("BIG", gen("big", gate, n=4, log=order), interruptible=True)
    small = jm.start("one poster", gen("s", n=2, log=order), priority=True)
    assert small.status == "queued" and jm.position(small) == 1
    gate.set()
    assert small.done.wait(5) and big.done.wait(5)
    assert small.status == "done" and big.status == "done"
    assert order.index("s0") < order.index("big3") and order.index("big0") < order.index("s0")     # in the middle, not at the end
    assert order == ["big0", "s0", "s1", "big1", "big2", "big3"]
    lines = [e.line() for e in big.events]
    assert any("paused for a quick change: one poster" in x for x in lines) and any("continuing" in x for x in lines)
    assert not big.problems and not small.problems                                    # the pause note is not a warning


def test_a_small_request_behind_a_run_that_cannot_pause_goes_to_the_front_of_the_line(tmp_path):
    jm = JobManager(tmp_path)
    gate, order = threading.Event(), []
    first = jm.start("restore", gen("r", gate, n=2, log=order))                          # not interruptible
    later = jm.start("big apply", gen("big", n=2, log=order), interruptible=True)
    small = jm.start("one poster", gen("s", n=1, log=order), priority=True)
    assert [j.title for j in jm.queued()] == ["one poster", "big apply"]
    gate.set()
    for j in (first, later, small):
        j.done.wait(5)
    assert order == ["r0", "r1", "s0", "big0", "big1"]


def test_cancelling_removes_a_waiting_run_but_a_started_one_cannot_be_stopped(tmp_path):
    jm = JobManager(tmp_path)
    gate = threading.Event()
    a = jm.start("A", gen("a", gate)); b = jm.start("B", gen("b"))
    assert jm.cancel(b.id) and b.status == "cancelled" and not jm.queued()
    assert not jm.cancel(a.id) and a.status == "running"
    gate.set(); a.done.wait(5)


def test_scheduled_runs_never_pile_up_behind_other_work(tmp_path):
    jm = JobManager(tmp_path)
    gate = threading.Event()
    jm.start("A", gen("a", gate))
    with pytest.raises(actions.Busy):
        jm.start("scheduled check", gen("x"), queue=False)
    assert not jm.queued()
    gate.set()


def test_a_failure_in_a_small_run_does_not_hurt_the_big_one_and_the_queue_keeps_moving(tmp_path):
    jm = JobManager(tmp_path)
    gate = threading.Event()

    def boom():
        raise RuntimeError("nope")
        yield

    big = jm.start("BIG", gen("big", gate, n=3), interruptible=True)
    small = jm.start("small", boom, priority=True)
    after = jm.start("after", gen("z", n=1))
    gate.set()
    for j in (big, small, after):
        assert j.done.wait(5)
    assert small.status == "failed" and "nope" in small.error and big.status == "done" and after.status == "done"


def test_the_runs_table_refreshes_itself_faster_while_something_is_running(tmp_path):
    import re
    import threading as th
    from fastapi.testclient import TestClient
    from laminator import actions as acts
    from laminator.web.app import create_app
    from test_web import FakeMdb, FakeServer, login
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    idle = c.get("/jobs").text
    assert 'id="runs"' in idle and 'hx-get="/jobs/table"' in idle and "every 8s" in idle                    # still checks, slowly, for runs started elsewhere
    part = c.get("/jobs/table", headers={"HX-Request": "true"}).text
    assert "<html" not in part and 'id="runs"' in part and "No runs since the UI started" in part
    gate = th.Event()

    def slow():
        gate.wait(5)
        yield acts.Event("apply", "x", "y")
    job = app.state.jobs.start("a slow run", slow)
    busy = c.get("/jobs/table").text
    assert "every 2s" in busy and "a slow run" in busy and ">running<" in busy
    gate.set(); job.done.wait(5)
    done = c.get("/jobs/table").text
    assert "every 8s" in done and ">done<" in done
