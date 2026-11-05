import re
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from laminator import cleanup, maintenance, store
from laminator import schedule as schedmod
from laminator.web.app import create_app
from test_posters_web import CFG, LookupMdb, PosterServer, finished, login

DAY = 86400


@pytest.fixture
def ui(tmp_path):
    srv = PosterServer()
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    sd = tmp_path / "servers" / "emby"
    (sd / "originals").mkdir(parents=True)
    st = store.Store(sd / "state.db")
    for i in ("1", "2", "9"):                                                  # 1 and 2 are on the server; 9 left it
        (sd / "originals" / f"{i}.img").write_bytes(b"ORIG" + i.encode())
        st.put_original(i, f"Item{i}", str(sd / "originals" / f"{i}.img"), "sha" + i, "t")
    st.mark_missing(["9"], time.time() - 40 * DAY)                              # missing for 40 days (the default grace is 30)
    return app, c, tok, tmp_path, sd, st


def test_the_cleanup_page_shows_the_plan_and_changes_nothing(ui):
    app, c, tok, root, sd, st = ui
    html = c.get("/cleanup").text
    assert "Item9" in html and "to trash" in html and "30 days" in html and "never causes a purge" in html
    assert "Clean up now" in html and (sd / "originals" / "9.img").exists() and "9" in st.managed_ids()


def test_running_the_cleanup_moves_the_item_to_trash_and_keeps_the_rest(ui):
    app, c, tok, root, sd, st = ui
    r = c.post("/cleanup/run", data={"srv": "emby", "token": tok()})
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "gone from the server" in log and "trash/" in log and "done" in log
    trash = next((sd / "trash").iterdir())
    assert (trash / "originals" / "9.img").read_bytes() == b"ORIG9" and "Item9" in (trash / "rows.json").read_text()
    assert not (sd / "originals" / "9.img").exists() and (sd / "originals" / "1.img").exists() and (sd / "originals" / "2.img").exists()
    st2 = store.Store(sd / "state.db")
    assert st2.managed_ids() == {"1", "2"} and "Item9" not in c.get("/cleanup").text


def test_an_offline_server_never_triggers_a_purge_from_the_ui(ui, monkeypatch):
    import httpx
    app, c, tok, root, sd, st = ui
    def down(self): raise httpx.ConnectError("down")
    monkeypatch.setattr(PosterServer, "libraries", down)
    log = finished(app, c, c.post("/cleanup/run", data={"srv": "emby", "token": tok()}).headers["location"])
    assert "could not list the server" in log
    assert (sd / "originals" / "9.img").exists() and "9" in store.Store(sd / "state.db").managed_ids()


def test_cleanup_actions_need_csrf_origin_and_a_password(ui, tmp_path):
    app, c, tok, root, sd, st = ui
    assert c.post("/cleanup/run", data={"srv": "emby", "token": ""}).status_code == 403
    assert c.post("/cleanup/run", data={"srv": "emby", "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/cleanup/run", data={"srv": "plex", "token": tok()}).status_code == 400
    assert (sd / "originals" / "9.img").exists()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app(dict(CFG), {"emby": PosterServer()}, tmp_path / "ro", None), follow_redirects=False)
    assert ro.post("/cleanup/run", data={"srv": "emby", "token": "x"}).status_code == 403
    assert ro.get("/cleanup").status_code == 200 and "Clean up now" not in ro.get("/cleanup").text


def test_the_periods_are_settings_and_are_validated(ui, monkeypatch):
    app, c, tok, root, sd, st = ui
    body = {"changed_every_minutes": "30", "maint_on": "1", "maint_day": "1", "maint_time": "03:00", "sched_template": "default", "token": tok()}
    r = c.post("/settings/general", data={**body, "grace_days": "14", "trash_days": "200"})
    assert "note=genok" in r.headers["location"]
    assert yaml.safe_load((root / "settings.yaml").read_text())["cleanup"] == {"grace_days": 14, "trash_days": 200}
    assert "14 days" in c.get("/cleanup").text and 'value="14"' in c.get("/settings").text
    for bad in ({"grace_days": "3"}, {"grace_days": "1000"}, {"trash_days": "x"}, {"trash_days": "5"}):
        r = c.post("/settings/general", data={**body, "grace_days": "30", "trash_days": "90", **bad, "token": tok()})
        assert "note=bad" in r.headers["location"], bad
    assert yaml.safe_load((root / "settings.yaml").read_text())["cleanup"] == {"grace_days": 14, "trash_days": 200}   # unchanged by the bad ones
    # a shorter grace period makes an item that has only been gone 20 days eligible
    st.mark_missing(["2"], time.time() - 20 * DAY)
    monkeypatch.setattr(PosterServer, "items", lambda self, lib=None, limit=None: [i for i in self.items_ if i.id != "2"] if lib else self.items_)
    c.post("/settings/general", data={**body, "grace_days": "14", "trash_days": "90", "token": tok()})
    assert "Item2" in c.get("/cleanup").text


def test_manual_maintenance_runs_the_pass_and_then_the_cleanup(ui):
    app, c, tok, root, sd, st = ui
    page = c.get("/confirm?action=maintain&scope=all&s=emby").text
    assert "full maintenance" in page and "trash/" in page and "EVERYTHING" in page
    assert c.get("/confirm?action=maintain&scope=item&item=1").status_code == 400
    body = {"action": "maintain", "scope": "all", "phrase": "EVERYTHING", "srv": "emby", "template": "default", "token": tok()}
    assert c.post("/jobs/start", data={**body, "phrase": "no"}).status_code == 400
    assert c.post("/jobs/start", data={**body, "scope": "item", "item": "1"}).status_code == 400
    log = finished(app, c, c.post("/jobs/start", data=body).headers["location"])
    assert "summary:" in log and "gone from the server" in log and "done" in log          # the pass, then the cleanup
    assert "9" not in store.Store(sd / "state.db").managed_ids()


def test_the_scheduled_maintenance_includes_the_cleanup_but_the_cheap_check_does_not(ui):
    app, c, tok, root, sd, st = ui
    app.state.scheduler.start_run("emby", "changed")
    log = finished(app, c, c.get("/jobs").text and re.search(r"/jobs/(?!table)([\w-]+)", c.get("/jobs").text).group(0))
    assert "only new or changed posters" in log and "gone from the server" not in log and (sd / "originals" / "9.img").exists()
    app.state.scheduler.start_run("emby", "maintenance")
    jobs = re.findall(r"/jobs/(?!table)([\w-]+)", c.get("/jobs").text)
    log = finished(app, c, f"/jobs/{jobs[0]}")
    assert "summary:" in log and "gone from the server" in log and not (sd / "originals" / "9.img").exists()
