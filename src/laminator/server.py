"""Emby / Jellyfin client. Both share most endpoints; differences live in subclasses."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from .config import ServerConfig


@dataclass
class Item:
    id: str
    name: str
    type: str
    year: int | None
    provider_ids: dict[str, str]
    has_poster: bool
    locked_fields: list[str] = field(default_factory=list)
    poster_tag: str | None = None
    lock_data: bool = False
    path: str | None = None  # the media file (movie) or folder (show) as the SERVER sees it
    library: str | None = None  # the library it was listed under (set by the caller)
    rating: str | None = None  # the content rating the server holds (OfficialRating)
    aspect: float | None = None  # the Primary image's width / height: about 0.67 for a poster, above 1 for a landscape video frame


class MediaServer:
    auth_header = "X-Emby-Token"
    lockable: frozenset | None = None  # LockedFields names the server accepts; None = every name laminator uses

    def __init__(self, cfg: ServerConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.http = client or httpx.Client(
            base_url=cfg.url, headers={self.auth_header: cfg.api_key}, timeout=30
        )

    def ping(self) -> str:
        """Verify the API key works. Returns 'ServerName version'. Raises httpx.HTTPStatusError (401) on a bad key."""
        r = self.http.get("/System/Info")
        r.raise_for_status()
        j = r.json()
        return f"{j.get('ServerName', 'server')} {j.get('Version', '')}".strip()

    def remote_images(self, item_id: str, kind: str = "Primary") -> list[dict]:
        """Candidate images from the providers configured ON the server (TheMovieDb, TheTVDB, FanArt, ...): url, thumbnail, size,
        language, rating, votes. Verified on Emby 4.10; the same shape is documented for Jellyfin (not yet verified there)."""
        r = self.http.get(f"/Items/{item_id}/RemoteImages",
                          params={"Type": kind, "IncludeAllLanguages": "true", "Limit": "400", "StartIndex": "0"}, timeout=90)
        r.raise_for_status()
        return r.json().get("Images", [])

    def download_remote_image(self, item_id: str, url: str, provider: str, kind: str = "Primary") -> None:
        """Ask the SERVER to fetch one of the URLs it listed and make it the item's image. The app never fetches the URL for this step."""
        r = self.http.post(f"/Items/{item_id}/RemoteImages/Download",
                           params={"Type": kind, "ProviderName": provider, "ImageUrl": url}, timeout=120)
        r.raise_for_status()

    def libraries(self) -> list[dict]:
        r = self.http.get("/Library/VirtualFolders")
        r.raise_for_status()
        return r.json()

    def _library_id(self, name: str) -> str:
        for lib in self.libraries():
            if lib.get("Name", "").lower() == name.lower():
                return lib["ItemId"]
        names = ", ".join(l.get("Name", "?") for l in self.libraries())
        raise LookupError(f"library {name!r} not found; available: {names}")

    def items(self, library: str | None = None, limit: int | None = None) -> list[Item]:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series",
            "Fields": "ProviderIds,LockedFields,LockData,ProductionYear,Path,OfficialRating,PrimaryImageAspectRatio",
        }
        if library:
            params["ParentId"] = self._library_id(library)
        if limit:
            params["Limit"] = str(limit)
        r = self.http.get("/Items", params=params)
        r.raise_for_status()
        return self._parse(r.json().get("Items", []))

    def _parse(self, raw) -> list[Item]:
        out = []
        for i in raw:
            out.append(
                Item(
                    id=str(i["Id"]),
                    name=str(i.get("Name", "")),
                    type=i.get("Type", ""),
                    year=i.get("ProductionYear"),
                    provider_ids={k.lower(): str(v) for k, v in (i.get("ProviderIds") or {}).items()},
                    has_poster="Primary" in (i.get("ImageTags") or {}),
                    locked_fields=list(i.get("LockedFields") or []),
                    poster_tag=(i.get("ImageTags") or {}).get("Primary"),
                    lock_data=bool(i.get("LockData")),
                    path=i.get("Path"),
                    rating=(i.get("OfficialRating") or None),
                    aspect=(float(i["PrimaryImageAspectRatio"]) if isinstance(i.get("PrimaryImageAspectRatio"), (int, float)) else None),
                )
            )
        return out

    def item(self, item_id: str) -> Item:
        for it in self.items_by_ids([item_id]):
            return it
        raise LookupError(item_id)

    def items_by_ids(self, ids: list[str]) -> list[Item]:
        r = self.http.get("/Items", params={"Ids": ",".join(ids), "Fields": "ProviderIds,LockedFields,LockData,ProductionYear,Path,OfficialRating,PrimaryImageAspectRatio"})
        r.raise_for_status()
        return self._parse(r.json().get("Items", []))

    _uid: str | None = None

    def user_id(self) -> str:
        if self._uid is None:
            r = self.http.get("/Users")
            r.raise_for_status()
            self._uid = r.json()[0]["Id"]
        return self._uid

    def get_full(self, item_id: str) -> dict:
        """The complete item DTO. The /Items list is partial and must not be posted back."""
        r = self.http.get(f"/Users/{self.user_id()}/Items/{item_id}")
        r.raise_for_status()
        return r.json()

    def post_item(self, item_id: str, body: dict) -> None:
        r = self.http.post(f"/Items/{item_id}", json=body)
        r.raise_for_status()  # 204 even when a field was silently ignored: callers must read back

    def rating_names(self, item_id: str) -> list[str]:
        r = self.http.get(f"/Items/{item_id}/MetadataEditor")
        r.raise_for_status()
        return [x["Name"] for x in r.json().get("ParentalRatingOptions", []) if x.get("Name")]

    def download_primary(self, item_id: str, max_width: int | None = None) -> tuple[bytes, str]:
        params = {"maxWidth": str(max_width)} if max_width else None
        r = self.http.get(f"/Items/{item_id}/Images/Primary", params=params)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")

    def upload_primary(self, item_id: str, data: bytes, content_type: str = "image/jpeg") -> None:
        # Both servers expect the image body base64-encoded, with the real image content type.
        r = self.http.post(
            f"/Items/{item_id}/Images/Primary",
            content=base64.b64encode(data),
            headers={"Content-Type": content_type},
        )
        r.raise_for_status()


class Emby(MediaServer):
    pass


class Jellyfin(MediaServer):
    auth_header = "Authorization"
    # Verified against Jellyfin 10.11.11: LockedFields only takes MetadataField names. Anything else (SortName, OriginalTitle, CriticRating, ...)
    # makes the whole item update fail with HTTP 400, so those fields are written without a lock.
    lockable = frozenset({"Name", "Overview", "OfficialRating", "Genres", "Tags", "Studios", "Cast", "Runtime", "ProductionLocations"})

    def __init__(self, cfg, client=None):
        if client is None:
            client = httpx.Client(
                base_url=cfg.url,
                headers={"Authorization": f'MediaBrowser Token="{cfg.api_key}"'},
                timeout=30,
            )
        super().__init__(cfg, client)


def connect(cfg: ServerConfig) -> MediaServer:
    return (Emby if cfg.kind == "emby" else Jellyfin)(cfg)
