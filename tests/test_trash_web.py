import re
import time

import pytest
from fastapi.testclient import TestClient

from laminator import cleanup, store, trash
from laminator.web.app import create_app
from test_cleanup_web import DAY, ui as _ui  # noqa: F401  (fixture: a UI with item 9 gone for 40 days)
from test_posters_web import CFG, PosterServer, finished

ui = _ui


def after_cleanup(app, c, tok):
    finished(app, c, c.post("/cleanup/run", data={"srv": "emby", "token": tok()}).headers["location"])


def test_the_trash_page_shows_what_cleanup_moved_and_how_long_it_is_kept(ui):
    app, c, tok, root, sd, st = ui
    assert "The trash is empty" in c.get("/trash").text
    after_cleanup(app, c, tok)
    html = c.get("/trash").text
    assert "Item9" in html and "Restore" in html and re.search(r"kept (89\.\d|90\.0) more day", html)          # 90 days from when it went in
    assert "nothing is overwritten" in html and "/cleanup" in html
    assert "trash" in c.get("/cleanup").text and "put things back" in c.get("/cleanup").text


def test_restoring_one_item_from_the_ui_brings_back_posters_and_records(ui):
    app, c, tok, root, sd, st = ui
    after_cleanup(app, c, tok)
    stamp = re.search(r'name="stamp" value="(\d{8}-\d{6})"', c.get("/trash").text).group(1)
    r = c.post("/trash/restore", data={"srv": "emby", "stamp": stamp, "item": "9", "token": tok()})
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "back from the trash" in log and "done" in log
    assert (sd / "originals" / "9.img").read_bytes() == b"ORIG9" and "9" in store.Store(sd / "state.db").managed_ids()
    assert "The trash is empty" in c.get("/trash").text


def test_restore_everything_needs_the_explicit_flag(ui):
    app, c, tok, root, sd, st = ui
    after_cleanup(app, c, tok)
    stamp = re.search(r'name="stamp" value="(\d{8}-\d{6})"', c.get("/trash").text).group(1)
    assert c.post("/trash/restore", data={"srv": "emby", "stamp": stamp, "token": tok()}).status_code == 400         # neither an item nor "everything"
    r = c.post("/trash/restore", data={"srv": "emby", "stamp": stamp, "everything": "1", "token": tok()})
    assert "done" in finished(app, c, r.headers["location"]) and (sd / "originals" / "9.img").exists()


def test_bad_input_csrf_origin_and_read_only(ui, tmp_path):
    app, c, tok, root, sd, st = ui
    after_cleanup(app, c, tok)
    stamp = re.search(r'name="stamp" value="(\d{8}-\d{6})"', c.get("/trash").text).group(1)
    base = {"srv": "emby", "stamp": stamp, "item": "9"}
    for bad in ({"stamp": "../etc"}, {"stamp": "20261101"}, {"item": "9; DROP"}, {"item": "../x"}, {"srv": "plex"}):
        assert c.post("/trash/restore", data={**base, **bad, "token": tok()}).status_code == 400, bad
    assert c.post("/trash/restore", data={**base, "token": ""}).status_code == 403
    assert c.post("/trash/restore", data={**base, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert not (sd / "originals" / "9.img").exists()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app(dict(CFG), {"emby": PosterServer()}, tmp_path / "ro", None), follow_redirects=False)
    assert ro.post("/trash/restore", data={**base, "token": "x"}).status_code == 403 and ro.get("/trash").status_code == 200
