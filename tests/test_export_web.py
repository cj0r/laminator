import io
import re
import zipfile
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator.web.app import create_app
from test_web import FakeMdb, FakeServer, finished, login

CFG = {"servers": {"emby": {"url": "http://x:8096"}}}


def jpg(c): 
    b = io.BytesIO(); Image.new("RGB", (30, 45), c).save(b, "JPEG"); return b.getvalue()


def mk(i, name, path, kind="Movie"):
    return NS(id=str(i), name=name, type=kind, year=2000, provider_ids={}, has_poster=True, poster_tag="t", path=path)


class ExportServer(FakeServer):
    def __init__(self):
        super().__init__()
        self.items_ = [mk(1, "Elf", "/mnt/movies/elf.1080p.mkv"), mk(2, "Dumbo", "/mnt/movies/dumbo.720p.mkv"),
                       mk(3, "Solo", "/mnt/movies/Solo (2018)/solo.2018.mkv"), mk(4, "Ozark", "/mnt/tv/Ozark", "Series")]

    def libraries(self): return [{"Name": "Movies", "CollectionType": "movies", "Locations": ["/mnt/movies"]},
                                  {"Name": "TV shows", "CollectionType": "tvshows", "Locations": ["/mnt/tv"]}]

    def items(self, lib=None, limit=None):
        return [i for i in self.items_ if (i.type == "Series") == (lib == "TV shows")] if lib else self.items_

    def download_primary(self, i, max_width=None): return jpg((int(i) * 40, 10, 10)), "image/jpeg"


@pytest.fixture
def ui(tmp_path):
    app = create_app(dict(CFG), {"emby": ExportServer()}, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path


def create(c, tok, **kw):
    return c.post("/export/create", data={"srv": "emby", "token": tok(), **kw})


def test_the_export_page_offers_the_options_and_lists_packages(ui):
    app, c, tok, root = ui
    html = c.get("/export").text
    assert "Create the package" in html and "No packages yet" in html and "Movies" in html and "TV shows" in html
    assert "next to the media file" in html and "removed by maintenance after 14 days" in html


def test_creating_a_package_builds_the_right_layout_and_serves_it_for_download(ui):
    app, c, tok, root = ui
    r = create(c, tok)
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "export" in log and "4 poster(s)" in log and "done" in log
    zips = list((root / "exports").glob("laminator-posters-emby-*.zip"))
    assert len(zips) == 1
    names = set(zipfile.ZipFile(zips[0]).namelist())
    assert names == {"movies/elf.1080p-poster.jpg", "movies/dumbo.720p-poster.jpg", "movies/Solo (2018)/poster.jpg", "tv/Ozark/poster.jpg",
                     "_laminator-export/manifest.csv", "_laminator-export/README.txt"}                     # flat -> by file name; own folder -> poster.jpg
    page = c.get("/export").text
    assert zips[0].name in page
    r = c.get(f"/exports/download/{zips[0].name}")
    assert r.status_code == 200 and r.content == zips[0].read_bytes() and "attachment" in r.headers["content-disposition"]
    assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None


def test_options_change_the_layout_and_one_library_can_be_chosen(ui):
    app, c, tok, root = ui
    finished(app, c, create(c, tok, library="TV shows", layout="library", naming="poster").headers["location"])
    z = zipfile.ZipFile(next((root / "exports").glob("*.zip")))
    assert {n for n in z.namelist() if not n.startswith("_")} == {"TV shows/Ozark/poster.jpg"}


def test_bad_options_and_unknown_things_are_refused(ui):
    app, c, tok, root = ui
    for bad in ({"which": "everything"}, {"layout": "../.."}, {"naming": "x"}):
        assert create(c, tok, **bad).status_code == 400, bad
    assert create(c, tok, library="Nope").status_code == 404
    assert c.post("/export/create", data={"srv": "plex", "token": tok()}).status_code == 400
    assert not (root / "exports").exists() or not list((root / "exports").glob("*.zip"))


def test_downloads_only_serve_real_packages_and_need_a_login(ui):
    app, c, tok, root = ui
    finished(app, c, create(c, tok).headers["location"])
    z = next((root / "exports").glob("*.zip"))
    (root / "exports" / "notes.txt").write_text("private")
    (root / "settings.yaml").write_text("x: 1")
    (root / "exports" / "laminator-posters-emby-20200101-000000.zip").symlink_to(root / "settings.yaml")
    for bad in ("notes.txt", "..%2Fsettings.yaml", "laminator-posters-emby-20200101-000000.zip", "laminator-posters-x.zip", "%2Fetc%2Fpasswd"):
        assert c.get(f"/exports/download/{bad}").status_code in (404, 307, 308), bad
    assert c.get(f"/exports/download/{z.name}").status_code == 200
    assert TestClient(app, follow_redirects=False).get(f"/exports/download/{z.name}").status_code == 303


def test_creating_a_package_needs_csrf_origin_and_a_password(ui, tmp_path):
    app, c, tok, root = ui
    assert create(c, tok, token="").status_code == 403
    assert c.post("/export/create", data={"srv": "emby", "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app(dict(CFG), {"emby": ExportServer()}, tmp_path / "ro", None), follow_redirects=False)
    assert ro.post("/export/create", data={"srv": "emby", "token": "x"}).status_code == 403
    assert ro.get("/export").status_code == 200 and "Create the package" not in ro.get("/export").text
