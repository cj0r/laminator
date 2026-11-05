import io
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator.web.app import create_app
from test_web import FakeMdb, FakeServer, login


class CountingServer(FakeServer):
    def __init__(self):
        super().__init__()
        self.asked = 0

    def download_primary(self, i, max_width=None):
        self.asked += 1
        buf = io.BytesIO(); Image.new("RGB", (1000, 1500), (120, 40, 200)).save(buf, "JPEG", quality=95)   # a big poster
        return buf.getvalue(), "image/jpeg"


@pytest.fixture
def thumbs(tmp_path):
    srv = CountingServer()
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, srv, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    return c, srv, tmp_path


def test_a_grid_thumbnail_is_small_made_once_and_kept_by_the_browser_for_good(thumbs):
    c, srv, root = thumbs
    r = c.get("/img/1/thumb?s=emby&v=t1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    w, h = Image.open(io.BytesIO(r.content)).size
    assert w <= 300 and h <= 900 and len(r.content) < 30_000                              # not the 179 KB original
    assert "immutable" in r.headers["cache-control"] and "31536000" in r.headers["cache-control"]
    assert srv.asked == 1 and (root / "cache" / "thumbs" / "emby-1-t1.jpg").is_file()
    again = c.get("/img/1/thumb?s=emby&v=t1")
    assert again.content == r.content and srv.asked == 1                                    # the server was not asked again


def test_a_wrong_or_missing_version_is_not_cached_for_long_and_a_changed_poster_replaces_the_thumbnail(thumbs, tmp_path):
    c, srv, root = thumbs
    assert "immutable" not in c.get("/img/1/thumb?s=emby").headers["cache-control"]
    assert "immutable" not in c.get("/img/1/thumb?s=emby&v=other").headers["cache-control"]
    assert (root / "cache" / "thumbs" / "emby-1-t1.jpg").is_file()
    srv.items_[0].poster_tag = "t9"                                                         # the poster changed in the server
    app2 = create_app({"server": {"kind": "emby", "url": "http://x"}}, srv, root, "pw", FakeMdb())   # (a fresh item list, not the 30 s one)
    c2 = TestClient(app2, follow_redirects=False); login(c2)
    before = srv.asked
    assert c2.get("/img/1/thumb?s=emby&v=t9").status_code == 200 and srv.asked == before + 1
    files = sorted(p.name for p in (root / "cache" / "thumbs").glob("emby-1-*.jpg"))
    assert files == ["emby-1-t9.jpg"]                                                       # the old one is gone, not piling up


def test_the_other_image_kinds_are_unchanged_and_unknown_kinds_are_refused(thumbs):
    c, srv, root = thumbs
    assert c.get("/img/1/current?s=emby").headers["cache-control"] == "private, max-age=60"
    assert c.get("/img/1/bogus?s=emby").status_code == 404 and c.get("/img/999/thumb?s=emby").status_code == 404
    assert c.get("/img/..%2F..%2Fetc/thumb?s=emby").status_code == 404
