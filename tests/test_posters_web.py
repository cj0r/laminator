import io
import re
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator import posters as pst
from laminator import store
from laminator.web import app as webapp
from laminator.web.app import create_app
from test_web import FakeMdb, FakeServer, finished, login

CFG = {"servers": {"emby": {"url": "http://x:8096"}}}


def jpg(color) -> bytes:
    b = io.BytesIO(); Image.new("RGB", (200, 300), color).save(b, "JPEG"); return b.getvalue()


INITIAL, CAND_A, CAND_B = jpg((10, 10, 10)), jpg((200, 30, 30)), jpg((30, 200, 30))


def remote(n_tmdb=2, extra=()):
    out = [{"ProviderName": "TheMovieDb", "Url": f"https://image.tmdb.org/t/p/original/p{i}.jpg", "ThumbnailUrl": f"https://image.tmdb.org/t/p/w154/p{i}.jpg",
            "Width": 2000, "Height": 3000, "CommunityRating": 9 - i * 0.01, "VoteCount": 10, "Language": "en" if i % 2 == 0 else "uk"} for i in range(n_tmdb)]
    out += [{"ProviderName": "TheTVDB", "Url": "https://artworks.thetvdb.com/p.jpg", "Width": 680, "Height": 1000, "CommunityRating": 3, "VoteCount": 1, "Language": "en"},
            {"ProviderName": "FanArt", "Url": "https://assets.fanart.tv/f.jpg", "CommunityRating": 0, "Language": ""},
            {"ProviderName": "Sneaky", "Url": "https://evil.example/x.jpg"}, {"ProviderName": "Plain", "Url": "http://image.tmdb.org/x.jpg"}]
    return out + list(extra)


class LookupMdb(FakeMdb):
    def lookup(self, *a): return {"score": 73, "ratings": []}


class PosterServer(FakeServer):
    """Stateful: the poster is real bytes and its tag changes whenever the poster does, like a media server."""

    def __init__(self):
        super().__init__()
        self.uploaded = {}                          # what was uploaded per item: another item must not be served our overlay
        self.tag, self.poster, self.remote, self.downloaded, self.refuse, self.next_image = 1, INITIAL, remote(), [], False, CAND_A

    @property
    def poster(self): return self._last

    @poster.setter
    def poster(self, value):                        # a test (or the "server") sets a new poster for everything
        self._last = self._base = value
        self.uploaded = {}

    def _it(self, base): return NS(**{**vars(base), "poster_tag": f"t{self.tag}"})
    def items(self, lib=None, limit=None): return [self._it(i) for i in self.items_]
    def items_by_ids(self, ids): return [self._it(i) for i in self.items_ if i.id in ids]
    def item(self, i): return next(x for x in self.items() if x.id == i)
    def download_primary(self, i, max_width=None): return self.uploaded.get(i, self._base), "image/jpeg"
    def upload_primary(self, i, data, ctype="image/jpeg"): self._last = data; self.uploaded[i] = data; self.tag += 1
    def remote_images(self, i, kind="Primary"): return self.remote
    def download_remote_image(self, i, url, provider):
        self.downloaded.append((i, url, provider))
        if not self.refuse:
            self.poster = self.next_image; self.tag += 1


@pytest.fixture
def ui(tmp_path, monkeypatch):
    srv = PosterServer()
    monkeypatch.setattr(pst, "fetch_image", lambda url, cache=None, **kw: CAND_A)
    monkeypatch.setattr(webapp, "POSTER_WAIT_TRIES", 3); monkeypatch.setattr(webapp, "POSTER_WAIT_STEP", 0.01)
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, srv


def cids(html, item="1"):
    return re.findall(rf"/posters/thumb/{item}/([\w-]+)\?s=", html)


def test_the_picker_lists_trusted_candidates_best_first_and_filters(ui):
    app, c, tok, root, srv = ui
    html = c.get("/posters/1?s=emby").text
    assert len(cids(html)) == 4 and "2 candidate(s) were left out" in html                    # tmdb x2, tvdb, fanart; the two unsafe ones dropped
    assert "evil.example" not in html and "http://image.tmdb.org" not in html and "image.tmdb.org/t/p/original" not in html   # no raw URLs reach the browser
    assert html.index("TMDb") < html.index("TVDB") and "fanart.tv" in html
    assert len(cids(c.get("/posters/1?s=emby&p=TVDB").text)) == 1
    assert len(cids(c.get("/posters/1?s=emby&lang=uk").text)) == 1 and len(cids(c.get("/posters/1?s=emby&lang=en").text)) == 2
    assert "Use&hellip;" in html or "Use…" in html
    srv.remote = remote(n_tmdb=30)
    p1, p2 = c.get("/posters/1?s=emby").text, c.get("/posters/1?s=emby&page_no=2").text
    assert len(cids(p1)) == 24 and len(cids(p2)) == 8 and not set(cids(p1)) & set(cids(p2)) and "page 2 of 2" in p2


def test_a_server_that_cannot_list_candidates_gives_a_message_not_a_crash(ui):
    import httpx
    app, c, tok, root, srv = ui
    def boom(i, kind="Primary"): raise httpx.ConnectError("down")
    srv.remote_images = boom
    r = c.get("/posters/1?s=emby")
    assert r.status_code == 200 and "could not ask the server" in r.text


def test_thumbnails_and_previews_only_work_for_ids_we_handed_out(ui):
    app, c, tok, root, srv = ui
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    r = c.get(f"/posters/thumb/1/{cid}?s=emby")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).width == 200
    assert Image.open(io.BytesIO(c.get(f"/posters/preview/1/{cid}?s=emby").content)).size[0] <= 600
    assert c.get("/posters/thumb/1/not-an-id?s=emby").status_code == 404
    assert c.get(f"/posters/thumb/2/{cid}?s=emby").status_code == 404                     # an id is bound to the item it was shown for
    assert c.get("/posters/thumb/1/https://evil.example/x.jpg?s=emby").status_code == 404   # a URL in place of an id means nothing


def test_a_failing_fetch_is_a_502_with_a_reason(ui, monkeypatch):
    app, c, tok, root, srv = ui
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    def refuse(url, cache=None, **kw): raise pst.PosterError("the image is too large")
    monkeypatch.setattr(pst, "fetch_image", refuse)
    r = c.get(f"/posters/thumb/1/{cid}?s=emby")
    assert r.status_code == 502 and "too large" in r.text and c.get(f"/posters/preview/1/{cid}?s=emby").status_code == 502


def use(c, tok, cid, **kw):
    return c.post("/posters/use", data={"srv": "emby", "item": "1", "cid": cid, "template": "default", "token": tok(), **kw})


def test_using_a_candidate_keeps_the_current_poster_first_then_replaces_and_overlays(ui):
    app, c, tok, root, srv = ui
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    page = c.get(f"/posters/use/1/{cid}?s=emby").text
    assert "Your current poster is saved first" in page and srv.downloaded == []         # confirmation only; nothing happened yet
    r = use(c, tok, cid)
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "kept as the original" in log and "is now the poster" in log and "done" in log
    assert srv.downloaded == [("1", "https://image.tmdb.org/t/p/original/p0.jpg", "TheMovieDb")]    # the URL the SERVER listed, not user input
    sd = root / "servers" / "emby"
    assert (sd / "originals" / "1.img").read_bytes() == INITIAL                            # the poster that was there is safe on disk
    adopted = [f for f in (sd / "originals").glob("1-*.img")]
    assert len(adopted) == 1 and adopted[0].read_bytes() == CAND_A                        # the candidate is the new original
    st = store.Store(sd / "state.db"); row = st.get("1")
    assert row["original_sha"] == __import__("hashlib").sha256(CAND_A).hexdigest() and row["applied_hash"]
    assert srv.poster not in (INITIAL, CAND_A)                                             # and the server now holds the overlay version
    assert Image.open(io.BytesIO(srv.poster)).size == (200, 300)


def test_choosing_again_keeps_every_earlier_poster(ui):
    app, c, tok, root, srv = ui
    first, second = cids(c.get("/posters/1?s=emby").text)[:2]
    finished(app, c, use(c, tok, first).headers["location"])
    srv.next_image = CAND_B
    finished(app, c, use(c, tok, second).headers["location"])
    files = sorted(f.read_bytes() for f in (root / "servers" / "emby" / "originals").glob("1*.img"))
    assert sorted([INITIAL, CAND_A, CAND_B]) == files                                      # the full history is on disk
    assert store.Store(root / "servers" / "emby" / "state.db").get("1")["original_sha"] == __import__("hashlib").sha256(CAND_B).hexdigest()


def test_if_the_server_refuses_the_image_nothing_is_lost(ui):
    app, c, tok, root, srv = ui
    srv.refuse = True
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    log = finished(app, c, use(c, tok, cid).headers["location"])
    assert "did not change the poster" in log and "with problems" in log
    assert (root / "servers" / "emby" / "originals" / "1.img").read_bytes() == INITIAL      # the original was saved before the attempt
    assert srv.poster == INITIAL


def test_unknown_ids_and_smuggled_urls_do_nothing(ui):
    app, c, tok, root, srv = ui
    cids(c.get("/posters/1?s=emby").text)
    r = use(c, tok, "made-up-id")
    assert r.status_code == 303 and r.headers["location"].startswith("/posters/1") and srv.downloaded == []
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    finished(app, c, use(c, tok, cid, url="https://evil.example/pwn.jpg", ImageUrl="https://evil.example/pwn.jpg").headers["location"])
    assert [d[1] for d in srv.downloaded] == ["https://image.tmdb.org/t/p/original/p0.jpg"]    # extra form fields are ignored
    assert c.post("/posters/use", data={"srv": "plex", "item": "1", "cid": cid, "token": tok()}).status_code == 400


def test_using_a_poster_needs_csrf_origin_and_a_password(ui, tmp_path):
    app, c, tok, root, srv = ui
    cid = cids(c.get("/posters/1?s=emby").text)[0]
    assert use(c, tok, cid, token="").status_code == 403
    assert c.post("/posters/use", data={"srv": "emby", "item": "1", "cid": cid, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert srv.downloaded == []
    (tmp_path / "ro").mkdir()
    ro_srv = PosterServer()
    ro = TestClient(create_app(dict(CFG), {"emby": ro_srv}, tmp_path / "ro", None), follow_redirects=False)
    html = ro.get("/posters/1?s=emby").text
    assert ro.get("/posters/1?s=emby").status_code == 200 and "Use&hellip;" not in html and "Use…" not in html   # browsing is fine read-only
    assert ro.get(f"/posters/use/1/{cids(html)[0]}?s=emby").status_code == 403
    assert ro.post("/posters/use", data={"srv": "emby", "item": "1", "cid": "x", "token": "x"}).status_code == 403
