"""Poster candidates and safe image fetching.

Candidates come from a PosterSource. The first (and, today, only) source is the media server's own providers
(TheMovieDb, TheTVDB, FanArt, ... as configured in Emby/Jellyfin), which needs no extra keys. Other sources can plug in by returning
Candidates the same way (MediUX has no public API or terms yet; see docs/PLAN.md), and they would pass through the same checks.

Everything a source returns is untrusted data. Before any image is fetched for a preview it must be https, on an allow-listed host,
resolve to a public address, stay within a size limit and decode as a real image. The browser never sees or supplies a URL: it only
ever holds an opaque candidate id."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

ALLOWED_HOSTS = ("image.tmdb.org", "artworks.thetvdb.com", "assets.fanart.tv")
MAX_BYTES = 15_000_000
MAX_PIXELS = 60_000_000
TTL = 30 * 60
CACHE_DAYS = 14
PLACEHOLDER_LIKES = 100_000


class PosterError(Exception):
    pass


@dataclass
class Candidate:
    cid: str
    provider: str
    url: str
    thumb_url: str
    width: int
    height: int
    language: str
    rating: float
    votes: int
    rating_type: str = "Score"  # "Score" (0-10, with votes) or "Likes" (a count): the two are not comparable

    @property
    def label(self) -> str:
        return {"TheMovieDb": "TMDb", "TheTVDB": "TVDB", "FanArt": "fanart.tv"}.get(self.provider, self.provider)


# ---- URL safety ----------------------------------------------------------------------------------------
def check_url(url: str) -> httpx.URL:
    """https, an allow-listed host, the default port, no credentials. Raises PosterError otherwise."""
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        raise PosterError("not a valid URL")
    if u.scheme != "https" or u.host not in ALLOWED_HOSTS or u.port not in (None, 443) or u.userinfo:
        raise PosterError(f"{u.host or url!r} is not an allowed image host")
    return u


def _public(host: str, resolve=socket.getaddrinfo) -> None:
    try:
        infos = resolve(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise PosterError(f"could not resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PosterError(f"{host} resolves to a non-public address; refusing")


def fetch_image(url: str, cache_dir: Path | None = None, client: httpx.Client | None = None, resolve=socket.getaddrinfo) -> bytes:
    """The bytes of an allow-listed image, verified to be a real image of sane size. Redirects are followed by hand and only to
    allow-listed hosts. Cached on disk when cache_dir is given."""
    check_url(url)
    cf = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".img")
        if cf.exists() and time.time() - cf.stat().st_mtime < CACHE_DAYS * 86400:
            return cf.read_bytes()
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": "laminator"})
    try:
        cur = url
        for _ in range(3):
            u = check_url(cur)
            _public(u.host, resolve)
            with client.stream("GET", cur) as r:
                if r.is_redirect:
                    cur = str(r.headers.get("location") or "")
                    if not cur.startswith("http"):
                        raise PosterError("bad redirect")
                    continue
                if r.status_code != 200:
                    raise PosterError(f"{u.host} answered HTTP {r.status_code}")
                if not r.headers.get("content-type", "").lower().startswith("image/"):
                    raise PosterError("the response is not an image")
                buf = bytearray()
                for chunk in r.iter_bytes(1 << 16):
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise PosterError("the image is too large")
                data = bytes(buf)
            break
        else:
            raise PosterError("too many redirects")
    except (httpx.HTTPError, httpx.InvalidURL) as e:  # InvalidURL: httpx rejects a malformed Location header itself
        raise PosterError(f"could not fetch the image ({type(e).__name__})")
    finally:
        if own:
            client.close()
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise PosterError("the image has too many pixels")
            im.verify()
    except PosterError:
        raise
    except Exception:
        raise PosterError("the data is not a valid image")
    if cf:
        cf.write_bytes(data)
    return data


def mid_size(url: str) -> str:
    """A medium-size version for previews where the host offers one (TMDb); otherwise the URL itself."""
    return url.replace("/t/p/original/", "/t/p/w780/") if httpx.URL(url).host == "image.tmdb.org" else url


def thumbnail(data: bytes, width: int = 300) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((width, width * 3))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
    return out.getvalue()


# ---- candidates ---------------------------------------------------------------------------------------------
def parse_candidates(raw: list[dict]) -> list[Candidate]:
    """Turn a server's remote-image list into candidates, dropping anything unsafe or malformed. Best-rated first."""
    out = []
    for r in raw:
        try:
            url = r["Url"]
            check_url(url)
            thumb = r.get("ThumbnailUrl") or url
            try:
                check_url(thumb)
            except PosterError:
                thumb = url
            rtype = "Likes" if str(r.get("RatingType") or "").lower() == "likes" else "Score"
            rating = float(r.get("CommunityRating") or 0)
            if rtype == "Likes" and rating >= PLACEHOLDER_LIKES:  # TheTVDB reports ~100000 for "no data"; that is not a rating
                rating = 0.0
            out.append(Candidate(secrets.token_urlsafe(9), str(r.get("ProviderName") or "?")[:40], url, thumb,
                                 int(r.get("Width") or 0), int(r.get("Height") or 0), str(r.get("Language") or "")[:8],
                                 rating, int(r.get("VoteCount") or 0), rtype))
        except (KeyError, PosterError, ValueError, TypeError):
            continue
    # real scores first (best, then most votes); like-counts after, by likes. The two scales are never compared.
    out.sort(key=lambda c: (c.rating_type == "Score", c.rating, c.votes), reverse=True)
    return out


class CandidateCache:
    """Remembers the candidates shown to the browser under opaque ids for a while, so the browser never supplies a URL."""

    def __init__(self, ttl: int = TTL, max_sets: int = 40):
        self.ttl, self.max_sets, self.sets = ttl, max_sets, {}

    def put(self, key: tuple, cands: list[Candidate]) -> None:
        now = time.time()
        self.sets = {k: v for k, v in self.sets.items() if now - v[0] < self.ttl}
        while len(self.sets) >= self.max_sets:
            self.sets.pop(min(self.sets, key=lambda k: self.sets[k][0]))
        self.sets[key] = (now, {c.cid: c for c in cands})

    def get(self, key: tuple, cid: str) -> Candidate | None:
        v = self.sets.get(key)
        if not v or time.time() - v[0] > self.ttl:
            return None
        return v[1].get(cid)
