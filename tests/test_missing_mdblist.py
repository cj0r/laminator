import re

import pytest
from fastapi.testclient import TestClient

from laminator import actions
from laminator.web.app import create_app
from test_assets_web import finished, first_item, jpg
from test_posters_web import CFG, PosterServer, login


def test_choose_score_without_a_key_says_so_instead_of_crashing():
    with pytest.raises(actions.SetupError, match="MDBList API key is not set"):
        actions.choose_score(type("It", (), {"id": "1", "type": "Movie", "provider_ids": {"imdb": "tt1"}})(), None, {"sources": ["aggregate"]}, {})


def test_a_run_without_an_mdblist_key_stops_first_with_a_plain_message_and_touches_nothing(tmp_path):
    srv = PosterServer()
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", None)               # no MDBList key saved
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    iid = first_item(c)
    r = c.post("/assets/upload", data={"srv": "emby", "item": iid, "token": tok()}, files={"file": ("p.jpg", jpg((250, 0, 0)), "image/jpeg")})
    log = finished(app, c, r.headers["location"])
    assert "MDBList API key is not set" in log and "AttributeError" not in log and "NoneType" not in log
    assert not srv.uploaded                                                          # nothing was written to the server
    assert not list((tmp_path / "servers" / "emby" / "originals").glob("*")) if (tmp_path / "servers" / "emby" / "originals").exists() else True


def test_every_page_but_settings_warns_when_the_mdblist_key_is_missing(tmp_path):
    app = create_app(dict(CFG), {"emby": PosterServer()}, tmp_path, "pw", None)
    c = TestClient(app, follow_redirects=False); login(c)
    assert "MDBList API key is not set" in c.get("/library").text and "MDBList API key is not set" in c.get("/status").text
    assert 'class="note bad">The MDBList API key is not set' not in c.get("/settings").text
