import hashlib
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from laminator import actions, backup, store
from laminator.web.app import create_app
from test_posters_web import CAND_A, CAND_B, CFG, INITIAL, LookupMdb, PosterServer, cids, finished, login, use
from test_posters_web import ui as _ui  # noqa: F401  (the fixture)

ui = _ui


def sha(b): return hashlib.sha256(b).hexdigest()


def names(root): return sorted(p.name for p in (root / "servers" / "emby" / "originals").glob("1*.img"))


def history(c): return c.get("/history/1?s=emby").text


def use_hist(c, tok, name, **kw):
    return c.post("/history/use", data={"srv": "emby", "item": "1", "name": name, "template": "default", "token": tok(), **kw})


def state_sha(root): return store.Store(root / "servers" / "emby" / "state.db").get("1")["original_sha"]


@pytest.fixture
def two_posters(ui):
    """Item 1 after using two candidates: INITIAL (first), CAND_A, CAND_B (current)."""
    app, c, tok, root, srv = ui
    a, b = cids(c.get("/posters/1?s=emby").text)[:2]
    finished(app, c, use(c, tok, a).headers["location"])
    srv.next_image = CAND_B
    finished(app, c, use(c, tok, b).headers["location"])
    time.sleep(0.01)
    return app, c, tok, root, srv


def test_an_untouched_item_has_no_history(ui):
    app, c, tok, root, srv = ui
    html = history(c)
    assert "Nothing saved yet" in html and "still the original" in html and "/history/use" not in html


def test_history_lists_every_distinct_poster_newest_first(two_posters):
    app, c, tok, root, srv = two_posters
    html = history(c)
    assert html.count("/history/thumb/1/") == 3
    assert html.count("current original") == 1 and html.count("first poster") >= 1
    entries = actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))
    assert [e["sha"] for e in entries] == [sha(CAND_B), sha(CAND_A), sha(INITIAL)]                 # newest first
    assert [e["current"] for e in entries] == [True, False, False] and [e["first"] for e in entries] == [False, False, True]
    assert f"/history/use/1/{entries[1]['name']}" in html and f"/history/use/1/{entries[2]['name']}" in html
    assert f"/history/use/1/{entries[0]['name']}" not in html                                       # you do not "go back" to where you are


def test_going_back_to_the_previous_poster_reuses_files_and_keeps_the_poster_you_left(two_posters):
    app, c, tok, root, srv = two_posters
    before = names(root)
    prev = actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))[1]
    page = c.get(f"/history/use/1/{prev['name']}?s=emby").text
    assert "stays in the history" in page and "overlay" in page
    assert re.search(r"Saved 20\d\d-\d\d-\d\d \d\d:\d\d", page)                                         # the date is shown
    log = finished(app, c, use_hist(c, tok, prev["name"]).headers["location"])
    assert "is the poster again" in log and "done" in log
    assert state_sha(root) == sha(CAND_A) and names(root) == before                                  # no duplicate file was written
    assert srv.poster not in (INITIAL, CAND_A, CAND_B)                                               # the server holds the overlay version
    entries = actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))
    assert len(entries) == 3 and [e["current"] for e in entries].count(True) == 1 and entries[0]["sha"] == sha(CAND_B)   # B is still there


def test_back_to_the_very_first_poster(two_posters):
    app, c, tok, root, srv = two_posters
    first = actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))[-1]
    assert first["name"] == "1.img" and first["first"]
    finished(app, c, use_hist(c, tok, "1.img").headers["location"])
    assert state_sha(root) == sha(INITIAL) and len(names(root)) == 3
    html = history(c)
    assert "Back to the very first poster" not in html and "Go back to the previous poster" in html   # first is current now
    assert actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))[-1]["current"]


def test_choosing_the_current_poster_just_rechecks_the_overlay(two_posters):
    app, c, tok, root, srv = two_posters
    cur = actions.poster_history(root / "servers" / "emby" / "originals", "1", state_sha(root))[0]
    assert "already the current original" in c.get(f"/history/use/1/{cur['name']}?s=emby").text
    log = finished(app, c, use_hist(c, tok, cur["name"]).headers["location"])
    assert "already the current original" in log and state_sha(root) == sha(CAND_B) and len(names(root)) == 3


def test_only_this_items_saved_posters_are_reachable(two_posters):
    app, c, tok, root, srv = two_posters
    (root / "servers" / "emby" / "originals" / "2.img").write_bytes(CAND_A)                           # another item's poster
    (root / "servers" / "emby" / "originals" / "12.img").write_bytes(CAND_A)
    (root / "servers" / "emby" / "originals" / "1-notahash.img").write_bytes(CAND_A)
    secret = root / "settings.yaml"; secret.write_text("x: 1\n")
    (root / "servers" / "emby" / "originals" / "1-deadbeef.img").symlink_to(secret)
    for bad in ("2.img", "12.img", "1-notahash.img", "1-deadbeef.img", "..%2F..%2Fsettings.yaml", "1.img.bak", "1-ABCDEF12.img", "1.IMG"):
        assert c.get(f"/history/thumb/1/{bad}?s=emby").status_code == 404, bad
        assert c.get(f"/history/preview/1/{bad}?s=emby").status_code == 404, bad
        assert c.get(f"/history/use/1/{bad}?s=emby").status_code == 404, bad
        assert use_hist(c, tok, bad).status_code == 404, bad
    html = history(c)
    assert "notahash" not in html and "deadbeef" not in html and html.count("/history/thumb/1/") == 3
    assert c.get("/history/thumb/1/1.img?s=emby").status_code == 200 and c.get("/history/preview/1/1.img?s=emby").status_code == 200


def test_history_actions_need_csrf_origin_and_a_password(two_posters, tmp_path):
    app, c, tok, root, srv = two_posters
    before = state_sha(root)
    assert use_hist(c, tok, "1.img", token="").status_code == 403
    assert c.post("/history/use", data={"srv": "emby", "item": "1", "name": "1.img", "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/history/use", data={"srv": "plex", "item": "1", "name": "1.img", "token": tok()}).status_code == 400
    assert state_sha(root) == before
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app(dict(CFG), {"emby": PosterServer()}, tmp_path / "ro", None), follow_redirects=False)
    assert ro.get("/history/1?s=emby").status_code == 200 and ro.get("/history/use/1/1.img?s=emby").status_code == 403
    assert ro.post("/history/use", data={"srv": "emby", "item": "1", "name": "1.img", "token": "x"}).status_code == 403


def test_the_order_of_the_history_survives_a_backup_and_restore(two_posters, tmp_path):
    app, c, tok, root, srv = two_posters
    od = root / "servers" / "emby" / "originals"
    for i, f in enumerate(sorted(od.glob("1*.img"))):                                               # give the files clearly different ages
        os.utime(f, (1_700_000_000 + i * 1000, 1_700_000_000 + i * 1000))
    os.utime(od / "1.img", (1_600_000_000, 1_600_000_000))                                          # the first poster is the oldest
    want = [e["sha"] for e in actions.poster_history(od, "1", state_sha(root))]
    rep = backup.create(root)
    new = tmp_path / "restored"
    backup.restore(rep.path, new)
    got = actions.poster_history(new / "servers" / "emby" / "originals", "1", state_sha(root))
    assert [e["sha"] for e in got] == want and got[-1]["first"] and got[-1]["name"] == "1.img"


def test_status_shows_keys_the_way_settings_does(tmp_path, monkeypatch):
    monkeypatch.delenv("API_MDBLIST", raising=False)
    monkeypatch.setenv("API_EMBY", "STATUSKEY-0123456789abcdef")
    app = create_app(dict(CFG), {"emby": PosterServer()}, tmp_path, "pw", LookupMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    html = c.get("/status").text
    assert "••••••••cdef" in html and "from .env / the environment" in html and "STATUSKEY-0123456789abcdef" not in html
    assert "API_EMBY:" not in html and "API_MDBLIST:" not in html                                    # no raw variable names as labels
    assert "not set" in html and "Set it in Settings" in html                                        # the missing MDBList key says so
    assert 'href="/settings"' in html
