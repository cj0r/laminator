"""The Custom posters page: the Library's views (posters, compact, list), search, a status filter and paging, with the title, library,
file and status of every poster readable in every view; and the thumbnail route that serves the files safely."""
import io
import os
import re
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator import schedule as schedmod
from laminator.web.app import create_app
from test_posters_web import CFG, LookupMdb, PosterServer, login


def jpg(size=(200, 300), color=(200, 10, 10)):
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, "JPEG"); return b.getvalue()


@pytest.fixture
def ui(tmp_path):
    srv = PosterServer()
    srv.items_ = [NS(id=str(n), name=f"Movie {n:03d}", type="Movie", year=2000 + n % 20, provider_ids={}, has_poster=True, poster_tag="t1") for n in range(1, 41)]
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    return app, c, tmp_path, srv


def folder(n):
    return f"Movie {n:03d} ({2000 + n % 20})"


def put(root, n, data=None, name="poster.jpg"):
    d = root / "assets" / folder(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data if data is not None else jpg())


def page(c, **q):
    return c.get("/assets", params={"s": "emby", **q})


def test_every_view_shows_title_library_file_and_status(ui):
    app, c, root, srv = ui
    put(root, 5); put(root, 7)
    (root / "assets" / "Stray File.jpg").write_bytes(jpg())
    marks = {"posters": 'class="grid assetgrid "', "compact": 'class="grid assetgrid compact"', "list": 'class="lrows"'}
    for view, mark in marks.items():
        html = page(c, view=view).text
        assert mark in html, view
        for needle in ("Movie 005", "Movie 007", "Movies", f"{folder(5)}/poster.jpg", "waiting for the next run", "Stray File.jpg", "not matched to any title"):
            assert needle in html, (view, needle)
        assert 'href="/item/5?s=emby"' in html and "/assets/thumb?rel=" in html
    assert "Stray File.jpg" in page(c, view="list").text and 'href="/item/' in page(c, view="list").text


def test_search_looks_at_title_library_and_file_and_htmx_gets_only_the_list(ui):
    app, c, root, srv = ui
    put(root, 5); put(root, 7); put(root, 9, name="poster.png")
    (root / "assets" / "Stray File.jpg").write_bytes(jpg())
    shown = lambda **q: set(re.findall(r"Movie 0\d\d(?= )|Stray File", page(c, **q).text.split('id="assetlist"')[1]))
    assert shown(q="005") == {"Movie 005"}
    assert shown(q="MOVIE 007") == {"Movie 007"}                                   # case does not matter
    assert shown(q="poster.png") == {"Movie 009"}                                    # the file name
    assert shown(q="movies") == {"Movie 005", "Movie 007", "Movie 009"}             # the library
    assert shown(q="stray") == {"Stray File"}
    assert "Nothing matches" in page(c, q="zzzz-no-such").text and "clear the search and filter" in page(c, q="zzzz-no-such").text
    part = c.get("/assets", params={"s": "emby", "q": "005"}, headers={"HX-Request": "true"}).text
    assert "<html" not in part and "Movie 005" in part and "Movie 007" not in part and 'id="assetlist"' not in part


def test_status_filter_and_counts(ui):
    app, c, root, srv = ui
    put(root, 1); put(root, 2); put(root, 3, data=b"this is not an image")
    (root / "assets" / "Stray.jpg").write_bytes(jpg())
    html = page(c).text
    for label, n in (("Every poster", 4), ("In use", 0), ("Waiting for the next run", 2), ("Unusable", 1), ("Not matched to any title", 1)):
        assert re.search(rf"{label} \({n}\)", html), (label, n)
    only = lambda st: set(re.findall(r"Movie 00\d(?= )|Stray", page(c, status=st).text.split('id="assetlist"')[1]))
    assert only("warn") == {"Movie 001", "Movie 002"} and only("bad") == {"Movie 003"} and only("unmatched") == {"Stray"} and only("ok") == set()
    assert only("nonsense") == {"Movie 001", "Movie 002", "Movie 003", "Stray"}      # an unknown filter is ignored
    assert "unusable" in page(c, status="bad").text.lower()


def test_paging_and_remembered_choices(ui):
    app, c, root, srv = ui
    for n in range(1, 31):
        put(root, n)
    r = page(c, per="25", view="compact")
    assert "page 1 of 2" in r.text and "Movie 025" in r.text and "Movie 026" not in r.text
    assert "laminator_view=compact" in r.headers["set-cookie"] and "laminator_assets_per=25" in r.headers["set-cookie"] or "compact" in c.cookies.get("laminator_view", "")
    assert 'href="/assets?s=emby&amp;q=&amp;status=&amp;view=compact&amp;per=25&amp;page_no=2"' in r.text or "page_no=2" in r.text
    second = page(c, page_no=2)                                                      # per and view come from the cookies now
    assert "page 2 of 2" in second.text and "Movie 026" in second.text and "Movie 030" in second.text and "grid assetgrid compact" in second.text
    assert "Movie 001" not in second.text
    assert "page 1 of 2" in page(c, page_no=-4).text and "page 2 of 2" in page(c, page_no=99).text      # out-of-range pages are clamped
    assert "30 posters" in page(c, per="not-a-number").text                          # a bad value falls back to the default (100 per page)


def test_names_are_escaped(ui):
    app, c, root, srv = ui
    (root / "assets").mkdir()
    (root / "assets" / "<i>bold</i> & co.jpg".replace("/", "")).write_bytes(jpg())       # no slash: a file name cannot hold one
    html = page(c).text
    assert "<i>bold" not in html and "&lt;i&gt;bold" in html
    assert "rel=%3Ci%3Ebold%3Ci%3E%20%26%20co.jpg" in html or "rel=%3Ci%3Ebold%3Ci%3E%20%26%20co.jpg" in html.replace("%2F", "/")


# ---- the thumbnail route --------------------------------------------------------------------------------------------
def test_thumbnails_are_small_cached_versioned_and_replaced_when_the_file_changes(ui):
    app, c, root, srv = ui
    put(root, 5, data=jpg((900, 1350)))
    rel = f"{folder(5)}/poster.jpg"
    r = c.get("/assets/thumb", params={"rel": rel})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "private, max-age=60"
    im = Image.open(io.BytesIO(r.content)); assert im.size[0] <= 300 and im.size[1] <= 450
    cache = root / "cache" / "asset-thumbs"
    first = list(cache.glob("*.jpg")); assert len(first) == 1
    v = os.stat(root / "assets" / rel).st_mtime_ns
    assert "immutable" in c.get("/assets/thumb", params={"rel": rel, "v": str(v)}).headers["cache-control"]
    f = root / "assets" / rel; f.write_bytes(jpg((600, 900), (0, 200, 0))); os.utime(f, ns=(v + 5_000_000_000, v + 5_000_000_000))
    assert c.get("/assets/thumb", params={"rel": rel}).status_code == 200
    after = list(cache.glob("*.jpg")); assert len(after) == 1 and after[0] != first[0]      # one thumbnail per file


def test_the_thumbnail_route_only_serves_image_files_inside_the_assets_folder(ui, tmp_path):
    app, c, root, srv = ui
    put(root, 5)
    (root / "assets" / ".hidden.jpg").write_bytes(jpg())
    (root / "assets" / "notes.txt").write_text("secret")
    outside = tmp_path / "outside.jpg"; outside.write_bytes(jpg())
    (root / "assets" / "escape.jpg").symlink_to(outside)
    (root / "settings.yaml").write_text("x: 1\n")
    (root / "assets" / "corrupt.jpg").write_bytes(b"not an image")
    for rel in ("", "/etc/passwd", "../settings.yaml", f"{folder(5)}/../../settings.yaml", ".hidden.jpg", "notes.txt", "nope.jpg", "escape.jpg",
                f"{folder(5)}", "a//b.jpg", "a/./b.jpg", "x" * 500 + ".jpg", "a/b/c/d/e/f/g.jpg", "poster.jpg\x00.txt"):
        assert c.get("/assets/thumb", params={"rel": rel}).status_code == 404, repr(rel)
    bad = c.get("/assets/thumb", params={"rel": "corrupt.jpg"})
    assert bad.status_code == 200 and bad.headers["content-type"] == "image/jpeg" and bad.headers["x-placeholder"] == "unreadable"   # a tile, not a broken image
    assert Image.open(io.BytesIO(bad.content)).size == (300, 450) and not list((root / "cache" / "asset-thumbs").glob("*corrupt*"))
    assert c.get("/assets/thumb", params={"rel": f"{folder(5)}/poster.jpg"}).status_code == 200


def test_thumbnails_need_a_login(ui):
    app, c, root, srv = ui
    put(root, 5)
    anon = TestClient(app, follow_redirects=False)
    r = anon.get("/assets/thumb", params={"rel": f"{folder(5)}/poster.jpg"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
