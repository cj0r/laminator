"""MDBList client with an on-disk cache (one JSON file per title)."""
from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
import os
import re
import time
from pathlib import Path

import httpx

BASE = "https://api.mdblist.com"
_SAFE = re.compile(r"[^a-z0-9]+")


class MDBListError(httpx.HTTPError):
    """A failed MDBList request, with a message that is safe to show and log. MDBList takes the key as a query parameter, so the text of a
    plain httpx error ("... for url '...?apikey=KEY'") would put the key into job logs, the UI and the server log: it is never used."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _hint(status: int) -> str:
    if status == 429:
        return "MDBList's request limit is used up (it resets daily). Titles finished so far are kept; run this again later"
    if status in (401, 403):
        return "MDBList rejected the API key: check it under Settings, MDBList"
    return f"MDBList answered HTTP {status}; try again later"


class MDBList:
    def __init__(self, api_key: str, cache_dir: Path = Path("cache/mdblist"), ttl_hours: float = 24):
        self.key = api_key
        self.dir = cache_dir
        self.ttl = ttl_hours * 3600
        self.http = httpx.Client(timeout=30)
        self.remaining: int | None = None

    def check(self) -> dict:
        """Verify the key. Returns the account's plan and request counters. Raises httpx.HTTPStatusError on a bad key (403)."""
        r = self._get(f"{BASE}/user")
        j = r.json()
        self.remaining = j.get("rate_limit_remaining", self.remaining)
        return {"plan": j.get("plan"), "used": j.get("api_requests_count"), "limit": j.get("api_requests"),
                "remaining": j.get("rate_limit_remaining")}

    def _get(self, url: str, allow_404: bool = False) -> httpx.Response:
        """GET with the key; any failure is raised as an MDBListError that never contains the request URL."""
        try:
            r = self.http.get(url, params={"apikey": self.key})
        except httpx.HTTPError as e:
            raise MDBListError(f"could not reach MDBList ({type(e).__name__})") from None
        if r.status_code >= 400 and not (allow_404 and r.status_code == 404):
            raise MDBListError(_hint(r.status_code), r.status_code) from None
        return r

    def _path(self, provider: str, kind: str, pid: str) -> Path:
        return self.dir / cache_name(provider, kind, pid)

    def lookup(self, provider: str, kind: str, pid: str) -> dict | None:
        """kind: 'movie' or 'show'. Returns the title JSON, or None if MDBList doesn't know it."""
        path = self._path(provider, kind, pid)
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            return json.loads(path.read_text())
        r = self._get(f"{BASE}/{provider}/{kind}/{pid}", allow_404=True)
        self.remaining = int(r.headers.get("x-ratelimit-remaining", -1))
        if r.status_code == 404:
            return None
        data = r.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return data


def cache_name(provider: str, kind: str, pid: str) -> str:
    return f"{_SAFE.sub('_', f'{provider}-{kind}-{pid}'.lower())}.json"


def cached_title(cache_dir: Path, provider: str, kind: str, pid: str) -> dict | None:
    """What a title's earlier lookup saved, however old (the Library filter reads this and never asks MDBList)."""
    try:
        return json.loads((cache_dir / cache_name(provider, kind, pid)).read_text())
    except (OSError, ValueError):
        return None


def score_pct(raw) -> int:
    """MDBList's 0-100 score rounded to a whole point (= one decimal on the 10-point scale), half up.
    This is the unit of change: 8.1 -> 8.2 counts, anything smaller does not. Decimal avoids binary float
    surprises at exact halves (73.5 must always give 74)."""
    return int(Decimal(str(raw)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def score_text(pct: int, decimals: int = 1) -> str:
    """73 -> '7.3' (decimals=1) or '73' (decimals=0, the 0-100 scale)."""
    return f"{pct / 10:.1f}" if decimals else str(pct)


AVERAGE_GAP = 5  # points on the 0-100 scale (5-9 point gaps checked by hand against the library: the average was right for all but two titles)


def aggregate_score(data: dict):
    """MDBList's own `score`, unless it is clearly dragged down by missing sources. That score is never higher than `score_average`
    (the mean of the sources that have a value) and normally equals it; when a title lacks several sources it falls far below it
    (Claydream: score 44, average 73 from sources reading 68-93, with three sources empty). A gap of AVERAGE_GAP points or more is
    taken as that effect and the average is used. A genuinely poor film has score == average, so it keeps its low score."""
    score, avg = data.get("score"), data.get("score_average")
    if isinstance(avg, (int, float)) and not isinstance(avg, bool) and avg > 0 and avg - (score or 0) >= AVERAGE_GAP:
        return avg
    return score


def pick_score(data: dict, sources: list[str], decimals: int = 1) -> tuple[str, str, int] | None:
    """Return (source, text, rounded 0-100 score) for the first listed source with a value, else None.
    Both the poster text and the CriticRating come from the same rounded value, so they change together."""
    by_src = {r.get("source"): r for r in data.get("ratings") or []}
    if data.get("score") is not None:  # MDBList's own aggregate of all sources, 0-100
        by_src["aggregate"] = {"score": aggregate_score(data)}
    for s in sources:
        r = by_src.get(s)
        if not r or not r.get("score"):  # no value, or 0: MDBList uses 0 for "no score", and a badge reading 0.0 would be wrong
            continue
        pct = score_pct(r["score"])
        return s, score_text(pct, decimals), pct
    return None
