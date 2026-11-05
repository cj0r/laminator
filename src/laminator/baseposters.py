"""Custom base posters: a poster you place for a title becomes the baseline laminator draws the score on,
instead of the poster the media server has. Typical use: fan edits, which live in their own folder.

Layout, under the assets folder (default <data>/assets, changeable in Settings), matched on the NAME OF THE MEDIA FOLDER:
    <assets>/<media folder>/poster.jpg            movie  /media/movies/Dumbo/dumbo.mkv  ->  <assets>/Dumbo/poster.jpg
    <assets>/<library>/<media folder>/poster.jpg  the same, kept apart per library (the library's name in the server)
    <assets>/<media folder>.jpg                   a flat file named after the folder also works
    <assets>/<file name without extension>.jpg    movies that share one folder: named after the file
jpg / jpeg / png / webp; names are compared without regard to upper/lower case. A show matches on its own folder.

Everything about a poster file is untrusted: names from the server never leave the assets folder, symlinks pointing elsewhere are ignored,
and the bytes must decode as a real image of sane size. A poster that already carries a laminator overlay is refused (it would stack)."""
from __future__ import annotations

import io
import os
import shutil
import time
from pathlib import Path, PurePosixPath

from PIL import Image

from .render import is_overlaid

EXTS = (".jpg", ".jpeg", ".png", ".webp")
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MAX_BYTES = 15_000_000
MAX_PIXELS = 60_000_000
MAX_FILES = 200_000
MAX_DEPTH = 4


class AssetError(Exception):
    pass


def assets_root(cfg: dict, data: Path) -> Path:
    v = cfg.get("assets_dir")
    return Path(v) if isinstance(v, str) and v.startswith("/") else (data / "assets").resolve()


def validate_dir(value: str) -> str:
    """The Settings value for the assets folder: an absolute path without .. parts (it need not exist yet)."""
    v = (value or "").strip()
    if not v.startswith("/") or "\x00" in v or len(v) > 500 or any(x == ".." for x in v.split("/")):
        raise ValueError("the folder must be an absolute path (start with /), without .. parts; in a container it is the path of the mounted volume")
    return v.rstrip("/") or "/"


def _clean(name: str | None) -> str | None:
    n = (name or "").strip()
    return n if n and n not in (".", "..") and "/" not in n and "\\" not in n and "\x00" not in n and len(n) <= 200 else None


def folder_name(it) -> str | None:
    """The media folder's name, as the server reports the path (only the last part is used, so a different mount point does not matter)."""
    p = (getattr(it, "path", None) or "").replace("\\", "/").rstrip("/")
    if p:
        parts = PurePosixPath(p)
        return _clean(parts.name if it.type != "Movie" else parts.parent.name)
    return _clean(f"{it.name} ({it.year})" if it.year else it.name)


def file_stem(it) -> str | None:
    p = (getattr(it, "path", None) or "").replace("\\", "/")
    return _clean(PurePosixPath(p).stem) if p and it.type == "Movie" else None


def keys_for(it) -> list[str]:
    """The lookup keys for an item, most specific first (lower-case, no extension)."""
    lib, folder, stem = _clean(getattr(it, "library", None)), folder_name(it), file_stem(it)
    out: list[str] = []
    for name, poster in ((folder, True), (stem, False)):
        if not name:
            continue
        for base in ([f"{lib}/{name}"] if lib else []) + [name]:
            out += ([f"{base}/poster", base] if poster else [base])
    return list(dict.fromkeys(k.lower() for k in out))


class AssetIndex:
    """Every usable poster file under the assets folder, keyed by its relative path without extension. Built once per run or page."""

    def __init__(self, root: Path):
        self.root, self.files = root, {}
        if not root.is_dir():
            return
        rr, n = root.resolve(), 0
        for dp, dirs, names in os.walk(root, followlinks=False):
            rel = Path(dp).relative_to(root)
            dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")] if len(rel.parts) < MAX_DEPTH else []
            for f in sorted(names):
                p = Path(dp) / f
                if p.suffix.lower() not in EXTS or f.startswith("."):
                    continue
                if p.is_symlink():
                    try:
                        p.resolve().relative_to(rr)  # a link that leaves the folder is ignored
                    except (ValueError, OSError):
                        continue
                n += 1
                if n > MAX_FILES:
                    return
                key = p.relative_to(root).with_suffix("").as_posix().lower()
                cur = self.files.get(key)
                if cur is None or EXTS.index(p.suffix.lower()) < EXTS.index(cur.suffix.lower()):
                    self.files[key] = p

    def find(self, it) -> Path | None:
        for k in keys_for(it):
            if k in self.files:
                return self.files[k]
        return None

    def key_of(self, path: Path) -> str:
        return path.relative_to(self.root).with_suffix("").as_posix().lower()

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def check_bytes(data: bytes) -> str:
    """Validate poster bytes; returns the file extension for the real format. Raises AssetError with a sentence."""
    if not data:
        raise AssetError("the file is empty")
    if len(data) > MAX_BYTES:
        raise AssetError(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
    try:
        im = Image.open(io.BytesIO(data))
        fmt, size = im.format, im.size
        im.verify()
    except Exception:
        raise AssetError("this is not a readable image")
    if fmt not in FORMATS:
        raise AssetError("use a JPEG, PNG or WebP image")
    if size[0] * size[1] > MAX_PIXELS:
        raise AssetError("the image is too large (more than 60 megapixels)")
    if is_overlaid(data):
        raise AssetError("this image already carries a laminator score overlay; use an original poster (an export of 'Originals' is fine)")
    return FORMATS[fmt]


def load(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise AssetError(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
        data = path.read_bytes()
    except OSError as e:
        raise AssetError(f"cannot read it ({e.strerror or type(e).__name__})")
    check_bytes(data)
    return data


def save(root: Path, idx: AssetIndex, it, data: bytes) -> Path:
    """Store an uploaded poster for an item: replaces the one that matches now, else <assets>/<media folder>/poster.<ext>."""
    ext = check_bytes(data)
    cur = idx.find(it)
    folder = folder_name(it)
    if cur is None and folder is None:
        raise AssetError("this item has no usable folder name to store a poster under")
    dest = (cur.with_suffix(ext) if cur is not None else root / folder / f"poster{ext}")
    new_dir = not dest.parent.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if new_dir:
        dest.parent.chmod(0o755)  # posters are not secret, and the folder may be shared with another app running as another user
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(dest)
    if cur is not None and cur != dest:
        cur.unlink(missing_ok=True)
    return dest


def remove(root: Path, data: Path, path: Path) -> Path:
    """Move a poster out of the assets folder into <data>/trash (never deleted outright)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise AssetError("that file is not inside the assets folder")
    dest = data / "trash" / ("assets-removed-" + time.strftime("%Y%m%d-%H%M%S")) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest
