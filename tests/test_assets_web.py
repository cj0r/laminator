import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator import schedule as schedmod
from laminator.web.app import create_app
from test_posters_web import CFG, LookupMdb, PosterServer, finished, login


def jpg(color=(200, 10, 10)):
    b = io.BytesIO(); Image.new("RGB", (200, 300), color).save(b, "JPEG"); return b.getvalue()


@pytest.fixture
def ui(tmp_path):
    srv = PosterServer()
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, srv


def first_item(c):
    return "1"   # Elf (2003); the other fake title, <script>alert(1)</script>, has slashes in its name and is rightly refused as a folder name


def test_the_assets_page_explains_the_layout_and_lists_nothing_at_first(ui):
    app, c, tok, root, srv = ui
    page = c.get("/assets?s=emby").text
    assert "Custom posters" in page and "Dumbo/poster.jpg" in page and str(root / "assets") in page and "No poster in the folder matches" in page


def test_uploading_a_custom_poster_stores_it_by_folder_name_and_runs_an_apply_that_uses_it(ui):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    mine = jpg((250, 0, 0))
    r = c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": tok()}, files={"file": ("p.jpg", mine, "image/jpeg")})
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    log = finished(app, c, r.headers["location"])
    files = list((root / "assets").rglob("poster.jpg"))
    assert len(files) == 1 and files[0].read_bytes() == mine
    assert "custom poster" in log.lower() and "done" in log
    page = c.get("/assets?s=emby").text
    assert files[0].parent.name in page and "in use" in page
    item_page = c.get(f"/item/{iid}?s=emby").text
    assert "Using" in item_page and "Remove the custom poster" in item_page


def test_bad_uploads_are_refused_and_nothing_is_stored(ui):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    for name, data in (("x.jpg", b"not an image"), ("x.gif", (lambda b: (Image.new("RGB", (9, 9)).save(b, "GIF"), b.getvalue())[1])(io.BytesIO()))):
        r = c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": tok()}, files={"file": (name, data, "image/jpeg")})
        assert r.status_code == 303 and "note=bad" in r.headers["location"]
    assert not (root / "assets").exists() or not list((root / "assets").rglob("*.*"))
    assert c.post("/assets/upload", data={"srv": "emby", "item": "nope", "token": tok()}, files={"file": ("a.jpg", jpg(), "image/jpeg")}).status_code == 404
    assert c.post("/assets/upload", data={"srv": "plex", "item": iid, "token": tok()}, files={"file": ("a.jpg", jpg(), "image/jpeg")}).status_code == 400


def test_a_file_placed_by_hand_is_matched_and_picked_up_by_the_use_custom_posters_run(ui):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    page = c.get(f"/item/{iid}?s=emby").text
    folder = re.search(r'<code>([^<]+)/poster\.jpg</code>', page).group(1)
    (root / "assets" / folder).mkdir(parents=True)
    (root / "assets" / folder / "poster.JPG").write_bytes(jpg((0, 0, 250)))
    (root / "assets" / "Nobody Has This.jpg").write_bytes(jpg())
    page = c.get("/assets?s=emby").text
    assert "waiting for the next run" in page and "Nobody Has This.jpg" in page and "Not matched to any title" in page
    r = c.post("/assets/apply", data={"srv": "emby", "token": tok()})
    assert "done" in finished(app, c, r.headers["location"])
    assert "in use" in c.get("/assets?s=emby").text


def test_removing_moves_the_file_to_the_trash_folder(ui):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    finished(app, c, c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": tok()}, files={"file": ("p.jpg", jpg(), "image/jpeg")}).headers["location"])
    r = c.post("/assets/remove", data={"srv": "emby", "item": iid, "token": tok()})
    assert "note=removed" in r.headers["location"] and not list((root / "assets").rglob("poster.*"))
    assert list((root / "trash").rglob("poster.jpg"))
    assert "note=nofile" in c.post("/assets/remove", data={"srv": "emby", "item": iid, "token": tok()}).headers["location"]


def test_the_folder_setting_is_validated_saved_and_can_be_reset(ui):
    app, c, tok, root, srv = ui
    for bad in ("relative/path", "/a/../b", "/x\x00y"):
        assert "note=bad" in c.post("/settings/assets", data={"path": bad, "token": tok()}).headers["location"], bad
    assert "note=assetsok" in c.post("/settings/assets", data={"path": str(root / "kometa-assets"), "token": tok()}).headers["location"]
    assert str(root / "kometa-assets") in c.get("/settings").text and str(root / "kometa-assets") in c.get("/assets?s=emby").text
    c.post("/settings/assets", data={"path": "", "token": tok()})
    assert str(root / "assets") in c.get("/assets?s=emby").text


def test_csrf_origin_and_read_only_apply(ui, tmp_path):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    files = {"file": ("a.jpg", jpg(), "image/jpeg")}
    assert c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": ""}, files=files).status_code == 403
    assert c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": tok()}, files=files, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/assets/remove", data={"srv": "emby", "item": iid, "token": ""}).status_code == 403
    assert c.post("/assets/apply", data={"srv": "emby", "token": ""}).status_code == 403
    assert not (root / "assets").exists()


def test_a_title_whose_name_cannot_be_a_folder_is_refused_not_mangled(ui):
    app, c, tok, root, srv = ui
    r = c.post("/assets/upload", data={"srv": "emby", "item": "2", "token": tok()}, files={"file": ("a.jpg", jpg(), "image/jpeg")})
    assert "note=bad" in r.headers["location"] and "usable+folder+name" in r.headers["location"]
    assert not (root / "assets").exists()


def test_a_title_with_no_poster_in_the_server_is_drawn_from_its_custom_poster_and_one_with_neither_is_skipped(ui):
    app, c, tok, root, srv = ui
    iid = first_item(c)
    other = next(i.id for i in srv.items_ if i.id != iid)
    for i in srv.items_:
        if i.id in (iid, other):
            i.has_poster = False                                    # fan edits often have no poster at all in the server
    page = c.get(f"/item/{iid}?s=emby").text
    folder = re.search(r'<code>([^<]+)/poster\.jpg</code>', page).group(1)
    (root / "assets" / folder).mkdir(parents=True)
    mine = jpg((0, 0, 250))
    (root / "assets" / folder / "poster.jpg").write_bytes(mine)
    r = c.post("/assets/apply", data={"srv": "emby", "token": tok()})
    log = finished(app, c, r.headers["location"])
    assert iid in srv.uploaded and other not in srv.uploaded        # the custom poster was drawn on and uploaded; the title with neither was not
    assert "no poster" in log and "done" in log
    saved = list((root / "servers" / "emby" / "originals").glob(f"{iid}*.img"))
    assert saved and saved[0].read_bytes() == mine                  # the custom poster is the saved original (there was no server poster to keep)


def test_apply_to_a_filtered_selection_touches_only_those_titles(ui):
    app, c, tok, root, srv = ui
    srv.items_[1].provider_ids = {"imdb": "tt2"}                                                      # the other title is scorable too, so only the selection keeps it out
    page = c.get("/library?s=emby").text
    assert "Apply to these" not in page and "Apply to this library" in page                          # nothing filtered: only the whole-library button
    page = c.get("/library?s=emby&q=elf").text
    link = re.search(r'href="(/confirm\?action=apply&(?:amp;)?scope=selection[^"]+)">Apply to these 1 title', page)
    assert link and "q=elf" in link.group(1)
    confirm = c.get(link.group(1).replace("&amp;", "&"))
    assert confirm.status_code == 200 and "Elf" in confirm.text and "Affects 1 item" in confirm.text and "Selection:" in confirm.text and "search" in confirm.text
    ids = re.search(r'name="ids" value="([^"]*)"', confirm.text).group(1)
    assert ids == "1"
    assert re.search(r'name="changed" value="1"\s*>', confirm.text) and 'checked' not in re.search(r'<input type="checkbox" name="changed"[^>]*>', confirm.text).group(0)   # a full refresh by default
    back = re.search(r'href="([^"]+)">Cancel', confirm.text).group(1).replace("&amp;", "&")
    assert back.startswith("/library?") and "q=elf" in back                                           # Cancel returns to the same filtered list
    r = c.post("/jobs/start", data={"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "ids": ids, "desc": "search 'elf'", "token": tok()})
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "done" in log and "Elf" in log and "1" in srv.uploaded and "2" not in srv.uploaded         # only the selected title was drawn
    assert "1 titles" in c.get("/jobs").text and "search" in c.get("/jobs").text                       # the run is named after its selection


def test_a_selection_is_checked_before_anything_runs(ui):
    app, c, tok, root, srv = ui
    srv.items_[1].provider_ids = {"imdb": "tt2"}
    base = {"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "token": tok()}
    assert c.post("/jobs/start", data={**base, "ids": ""}).status_code == 400                         # nothing selected
    assert c.post("/jobs/start", data={**base, "ids": "999,abc"}).status_code == 400                   # ids the server does not have are dropped: nothing left
    r = c.post("/jobs/start", data={**base, "ids": "1,999"})                                            # unknown ids are ignored, known ones kept
    assert r.status_code == 303 and "done" in finished(app, c, r.headers["location"]) and "2" not in srv.uploaded
    assert c.post("/jobs/start", data={**base, "action": "restore", "ids": "1", "what": "all"}).status_code == 400    # a selection restores posters and CriticRating only
    assert c.post("/jobs/start", data={**base, "action": "maintain", "ids": "1"}).status_code == 400
    assert c.get("/confirm?action=apply&scope=selection&lib=Movies&s=emby&q=zzzz-no-match").status_code == 400
    assert c.get("/confirm?action=restore&scope=selection&lib=Movies&s=emby&q=zzzz-no-match").status_code == 400


def test_restoring_a_selection_puts_back_only_those_posters(ui):
    app, c, tok, root, srv = ui
    srv.items_[1].provider_ids = {"imdb": "tt2"}
    original = srv.poster
    for i in ("1", "2"):                                                                                # draw both, then restore one of them
        r = c.post("/jobs/start", data={"action": "apply", "scope": "item", "item": i, "srv": "emby", "template": "default", "token": tok()})
        assert "done" in finished(app, c, r.headers["location"])
    assert srv.uploaded["1"] != original and srv.uploaded["2"] != original
    confirm = c.get("/confirm?action=restore&scope=selection&what=scores&lib=Movies&s=emby&q=elf")
    assert confirm.status_code == 200 and "Restore the original poster of 1 selected title" in confirm.text and "not touched" in confirm.text
    ids = re.search(r'name="ids" value="([^"]*)"', confirm.text).group(1)
    assert ids == "1" and 'name="what" value="scores"' in confirm.text
    r = c.post("/jobs/start", data={"action": "restore", "scope": "selection", "what": "scores", "srv": "emby", "ids": ids, "desc": "search 'elf'", "token": tok()})
    log = finished(app, c, r.headers["location"])
    assert "done" in log and "restore" in log and "Elf" in log
    assert srv.uploaded["1"] == original and srv.uploaded["2"] != original                              # only the selected title went back


def test_an_old_zero_badge_is_taken_off_when_the_title_still_has_no_real_score(tmp_path):
    import sqlite3
    from test_posters_web import FakeMdb as _Fake
    class ZeroMdb(_Fake):
        def lookup(self, *a): return {"score": 0, "ratings": []}                                      # MDBList: no real score
    srv = PosterServer()
    srv.items_[1].provider_ids = {"imdb": "tt2"}
    original = srv.poster
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    for i in ("1", "2"):
        r = c.post("/jobs/start", data={"action": "apply", "scope": "item", "item": i, "srv": "emby", "template": "default", "token": tok()})
        assert "done" in finished(app, c, r.headers["location"])
    db = sqlite3.connect(tmp_path / "servers" / "emby" / "state.db")
    db.execute("UPDATE posters SET score_text='0.0' WHERE item_id='1'"); db.commit(); db.close()      # what the old code left on title 1
    drawn_2 = srv.uploaded["2"]
    app2 = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", ZeroMdb())
    c2 = TestClient(app2, follow_redirects=False); login(c2)
    tok2 = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c2.get("/settings").text).group(1)
    r = c2.post("/jobs/start", data={"action": "apply", "scope": "library", "lib": "Movies", "srv": "emby", "template": "default", "token": tok2()})
    log = finished(app2, c2, r.headers["location"])
    assert srv.uploaded["1"] == original and "0.0 badge" in log                                      # the stale badge is gone, the original is back
    assert srv.uploaded["2"] == drawn_2 and "no score from MDBList" in log                           # a title whose real badge is fine is left as it was
