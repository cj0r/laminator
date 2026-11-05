import io
import socket
import time

import httpx
import pytest
from PIL import Image

from laminator import posters as pst
from laminator.posters import PosterError


def img(w=20, h=30, color=(10, 20, 30), fmt="JPEG") -> bytes:
    b = io.BytesIO(); Image.new("RGB", (w, h), color).save(b, fmt); return b.getvalue()


PUBLIC = lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.1", 443))]


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


GOOD = "https://image.tmdb.org/t/p/original/abc.jpg"


# ---- which URLs are acceptable at all --------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "http://image.tmdb.org/t/p/original/a.jpg", "https://evil.example/a.jpg", "https://image.tmdb.org.evil.example/a.jpg",
    "https://user:pw@image.tmdb.org/a.jpg", "https://image.tmdb.org:8443/a.jpg", "file:///etc/passwd", "javascript:alert(1)",
    "ftp://image.tmdb.org/a", "//image.tmdb.org/a.jpg", "https://127.0.0.1/a.jpg", "https://localhost/a.jpg", "", "not a url",
    "https://image.tmdb.org@evil.example/a.jpg", "https://IMAGE.TMDB.ORG.evil.example/x"])
def test_unsafe_urls_are_refused(bad):
    with pytest.raises(PosterError):
        pst.check_url(bad)


def test_allowed_hosts_pass():
    for ok in (GOOD, "https://artworks.thetvdb.com/banners/posters/1.jpg", "https://assets.fanart.tv/fanart/movies/1/movieposter/a.jpg",
               "https://IMAGE.TMDB.ORG/t/p/w154/a.jpg"):
        assert pst.check_url(ok)


def test_candidates_are_filtered_cleaned_and_ranked():
    raw = [
        {"ProviderName": "TheMovieDb", "Url": GOOD, "ThumbnailUrl": "https://image.tmdb.org/t/p/w154/abc.jpg", "Width": 2000, "Height": 3000,
         "CommunityRating": 5.5, "VoteCount": 3, "Language": "en", "RatingType": "Score"},
        {"ProviderName": "TheMovieDb", "Url": "https://image.tmdb.org/t/p/original/b.jpg", "CommunityRating": 8.1, "VoteCount": 40, "RatingType": "Score"},
        {"ProviderName": "TheTVDB", "Url": "https://artworks.thetvdb.com/p.jpg", "Width": "1000", "Height": "1500", "CommunityRating": 100002, "VoteCount": 0, "RatingType": "Likes"},
        {"ProviderName": "FanArt", "Url": "https://assets.fanart.tv/f.jpg", "ThumbnailUrl": None, "CommunityRating": 10, "Language": "fr", "RatingType": "Likes"},
        {"ProviderName": "FanArt", "Url": "https://assets.fanart.tv/g.jpg", "CommunityRating": 3, "RatingType": "Likes"},
        {"ProviderName": "Evil", "Url": "https://evil.example/x.jpg"},
        {"ProviderName": "Evil2", "Url": "http://image.tmdb.org/x.jpg"},
        {"ProviderName": "NoUrl"}, {"Url": 5}, "garbage", {"ProviderName": "TheTVDB", "Url": "https://artworks.thetvdb.com/q.jpg", "Width": "wide"},
        {"ProviderName": "TheMovieDb", "Url": GOOD, "ThumbnailUrl": "https://evil.example/t.jpg", "CommunityRating": 1},
    ]
    got = pst.parse_candidates(raw)
    assert [(c.provider, c.rating_type, c.rating) for c in got] == [
        ("TheMovieDb", "Score", 8.1), ("TheMovieDb", "Score", 5.5), ("TheMovieDb", "Score", 1.0),     # real scores first, best first
        ("FanArt", "Likes", 10.0), ("FanArt", "Likes", 3.0), ("TheTVDB", "Likes", 0.0)]                  # then like-counts; TVDB's 100002 is "no data"
    tvdb = got[-1]
    assert tvdb.width == 1000 and tvdb.thumb_url == tvdb.url                                              # no thumbnail: use the image itself
    assert got[2].thumb_url == got[2].url                                                                 # an untrusted thumbnail host is replaced
    assert len({c.cid for c in got}) == 6 and all(len(c.cid) >= 10 for c in got) and tvdb.label == "TVDB"


# ---- fetching ----------------------------------------------------------------------------------------------
def ok_handler(req):
    return httpx.Response(200, content=img(), headers={"content-type": "image/jpeg"})


def test_a_good_image_is_fetched_verified_and_cached(tmp_path):
    calls = []
    def h(req):
        calls.append(str(req.url)); return ok_handler(req)
    data = pst.fetch_image(GOOD, tmp_path, client(h), PUBLIC)
    assert Image.open(io.BytesIO(data)).size == (20, 30)
    assert pst.fetch_image(GOOD, tmp_path, client(h), PUBLIC) == data and len(calls) == 1          # second time: from disk
    assert len(list(tmp_path.glob("*.img"))) == 1


def test_hostile_responses_are_refused(tmp_path):
    cases = {
        "not an image type": httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
        "claims image, is not": httpx.Response(200, content=b"GIF89 definitely not", headers={"content-type": "image/jpeg"}),
        "http error": httpx.Response(404),
        "too large": httpx.Response(200, content=b"\xff" * (pst.MAX_BYTES + 10), headers={"content-type": "image/jpeg"}),
    }
    for name, resp in cases.items():
        with pytest.raises(PosterError):
            pst.fetch_image(GOOD, None, client(lambda r, resp=resp: resp), PUBLIC)
    bomb = img(9000, 9000)                                                                           # 81 million pixels, tiny file
    with pytest.raises(PosterError, match="pixels"):
        pst.fetch_image(GOOD, None, client(lambda r: httpx.Response(200, content=bomb, headers={"content-type": "image/jpeg"})), PUBLIC)


def test_a_host_that_resolves_to_a_private_address_is_refused():
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "172.16.0.9", "0.0.0.0"):
        resolve = lambda host, port, **kw: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
        with pytest.raises(PosterError, match="non-public"):
            pst.fetch_image(GOOD, None, client(ok_handler), resolve)
    def boom(host, port, **kw): raise OSError("nope")
    with pytest.raises(PosterError, match="resolve"):
        pst.fetch_image(GOOD, None, client(ok_handler), boom)


def test_redirects_only_go_to_allowed_hosts_and_are_limited():
    def redirect_to(target):
        def h(req):
            if str(req.url) == GOOD:
                return httpx.Response(302, headers={"location": target})
            return ok_handler(req)
        return h
    assert pst.fetch_image(GOOD, None, client(redirect_to("https://artworks.thetvdb.com/x.jpg")), PUBLIC)       # allowed hop
    for bad in ("https://evil.example/x.jpg", "http://image.tmdb.org/x.jpg", "https://169.254.169.254/latest", "/relative", "javascript:1"):
        with pytest.raises(PosterError):
            pst.fetch_image(GOOD, None, client(redirect_to(bad)), PUBLIC)
    with pytest.raises(PosterError, match="redirects"):
        pst.fetch_image(GOOD, None, client(lambda r: httpx.Response(302, headers={"location": GOOD})), PUBLIC)


def test_network_errors_become_poster_errors():
    def h(req): raise httpx.ConnectError("down")
    with pytest.raises(PosterError, match="ConnectError"):
        pst.fetch_image(GOOD, None, client(h), PUBLIC)


def test_helpers():
    assert pst.mid_size("https://image.tmdb.org/t/p/original/a.jpg") == "https://image.tmdb.org/t/p/w780/a.jpg"
    assert pst.mid_size("https://artworks.thetvdb.com/p.jpg") == "https://artworks.thetvdb.com/p.jpg"
    t = pst.thumbnail(img(1000, 1500), 300)
    assert Image.open(io.BytesIO(t)).width == 300
    assert Image.open(io.BytesIO(pst.thumbnail(img(100, 150, fmt="PNG"), 300))).format == "JPEG"


def test_the_candidate_cache_expires_and_is_bounded():
    c = pst.CandidateCache(ttl=0.2, max_sets=2)
    a = pst.parse_candidates([{"ProviderName": "TheMovieDb", "Url": GOOD}])
    c.put(("emby", "1"), a)
    assert c.get(("emby", "1"), a[0].cid) is a[0] and c.get(("emby", "2"), a[0].cid) is None and c.get(("emby", "1"), "nope") is None
    c.put(("emby", "2"), a); c.put(("emby", "3"), a)
    assert len(c.sets) == 2 and ("emby", "1") not in c.sets                                           # oldest evicted
    time.sleep(0.3)
    assert c.get(("emby", "3"), a[0].cid) is None                                                       # expired
