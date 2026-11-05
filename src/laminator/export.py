"""Export posters as a zip laid out so that, extracted at the right folder, each image lands next to the media it belongs to:

    movies/movie-a/movie.a.1080p.mkv   ->   movies/movie-a/poster.jpg
    movies/elf.1080p.mkv               ->   movies/elf.1080p-poster.jpg     (several films in one folder: named after the file)
    tv/Ozark/                          ->   tv/Ozark/poster.jpg

Paths come from the media server and are untrusted: each one must lie inside one of the library's folders and contain no `..`,
empty or NUL components, or the item is skipped (and listed in the manifest). Nothing is ever written outside the package."""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator

from PIL import Image

from . import actions
from .actions import Event

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".iso", ".strm", ".mpg", ".mpeg", ".flv",
             ".webm", ".divx", ".vob", ".ogm", ".3gp", ".rmvb"}
IMG_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
LAYOUTS = ("location", "library", "none")
NAMINGS = ("auto", "poster", "stem")
WHICH = ("current", "original")
DIR = "_laminator-export"


@dataclass
class Entry:
    item: object
    library: str
    top: str
    dir_rel: str
    stem: str | None  # file name without extension for a movie file; None for a folder item (show, disc folder)
    server_path: str
    use_stem: bool = False


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (item name, server path, reason)


def _norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/")


def _within(path: str, location: str) -> str | None:
    """`path` relative to `location`, or None if it is not strictly inside it or any component is unsafe."""
    p, loc = _norm(path), _norm(location)
    if not loc or not p.startswith(loc + "/"):
        return None
    parts = p[len(loc) + 1:].split("/")
    if any(x in ("", ".", "..") or "\x00" in x for x in parts):
        return None
    return "/".join(parts)


def plan_export(libraries: list[dict], items_by_library: dict[str, list], layout: str = "location", naming: str = "auto",
                only_library: str | None = None) -> Plan:
    """Decide, for every poster, which folder inside the package it goes to and what it is called."""
    if layout not in LAYOUTS or naming not in NAMINGS:
        raise ValueError("unknown layout or naming")
    plan = Plan()
    locs = {v["Name"]: [str(x) for x in (v.get("Locations") or [])] for v in libraries}
    for lib, items in items_by_library.items():
        if only_library and lib != only_library:
            continue
        for it in items:
            path = getattr(it, "path", None)
            if not path:
                plan.skipped.append((it.name, "", "the server reports no file path for this item"))
                continue
            best = None
            for loc in locs.get(lib, []):
                rel = _within(path, loc)
                if rel is not None and (best is None or len(loc) > len(best[0])):
                    best = (loc, rel)
            if best is None:
                plan.skipped.append((it.name, path, "not inside one of the library's folders (or an unsafe path)"))
                continue
            loc, rel = best
            parts = rel.split("/")
            is_file = PurePosixPath(parts[-1]).suffix.lower() in VIDEO_EXT
            dir_rel = "/".join(parts[:-1]) if is_file else rel
            stem = PurePosixPath(parts[-1]).stem if is_file else None
            top = {"location": PurePosixPath(_norm(loc)).name, "library": lib, "none": ""}[layout]
            plan.entries.append(Entry(it, lib, top, dir_rel, stem, path))
    # naming: a bare poster.jpg only when the folder holds one film; otherwise <file>-poster.jpg so nothing overwrites anything
    groups: dict[tuple, int] = {}
    for e in plan.entries:
        if e.stem is not None:
            groups[(e.top, e.dir_rel)] = groups.get((e.top, e.dir_rel), 0) + 1
    for e in plan.entries:
        if e.stem is None:
            e.use_stem = False
        elif naming == "stem":
            e.use_stem = True
        elif naming == "poster":
            e.use_stem = False
        else:
            e.use_stem = groups[(e.top, e.dir_rel)] > 1 or e.dir_rel == ""
    return plan


def arcname(e: Entry, ext: str) -> str:
    name = f"{e.stem}-poster.{ext}" if e.use_stem else f"poster.{ext}"
    return "/".join(x for x in (e.top, e.dir_rel, name) if x)


def _cell(v) -> str:
    """CSV cells that a spreadsheet could run as a formula get a leading quote (item names are untrusted text)."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


README = """laminator poster export
========================

Made {when} from {server}. {n} poster(s). Source: {which}.

HOW TO USE
  Extract this zip into the folder that CONTAINS your library folders, so that each poster lands next to its media.
  Example: if your movies are in /media/movies, extract into /media and /media/movies/movie-a/poster.jpg appears
  beside /media/movies/movie-a/movie.a.1080p.mkv.

  The paths inside the zip were taken from what the MEDIA SERVER sees ({locations}).
  If your files are mounted somewhere else on your computer, extract into the folder that matches.
  The top folder name is {top_note}.

NOTES
  * Extracting overwrites existing files with the same name (for example an existing poster.jpg). Look before you extract.
  * Where several films share one folder the image is named <film file>-poster.jpg (read by Emby, Jellyfin and Kodi);
    a folder that holds a single film, or a show, gets poster.jpg.
  * manifest.csv (next to this file) lists every item, where its poster went, and anything that was skipped and why.
  * This {dir}/ folder can be deleted after extracting.
"""


def build(ws, dest: Path, plan: Plan, which: str = "current", layout: str = "location", server_label: str = "") -> Iterator[Event]:
    """Write the zip (event per step). `which`: 'current' = the poster as it is on the server now (with your overlay),
    'original' = the saved original without any overlay (falls back to the current one for items laminator never touched)."""
    if which not in WHICH:
        raise ValueError("which must be current or original")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    rows, used, done = [], {}, 0
    yield Event("info", message=f"exporting {len(plan.entries)} poster(s) ({'as shown now' if which == 'current' else 'originals, no overlay'})...")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for i, e in enumerate(plan.entries, 1):
                it = e.item
                try:
                    if not it.has_poster:
                        rows.append((it.name, e.library, e.server_path, "", "", "skipped: the item has no poster"))
                        continue
                    row = ws.store.get(it.id)
                    f = actions.original_file(row, ws.originals) if (which == "original" and row) else None
                    data = f.read_bytes() if f is not None and f.exists() else ws.srv.download_primary(it.id)[0]
                    source = "original" if f is not None and f.exists() else "server"
                    with Image.open(io.BytesIO(data)) as im:
                        ext = IMG_EXT.get(im.format or "")
                    if not ext:
                        raise ValueError("not a JPEG/PNG/WebP/GIF image")
                except Exception as ex:
                    rows.append((it.name, e.library, e.server_path, "", "", f"skipped: {type(ex).__name__}: {ex}"))
                    yield Event("warn", it.name, f"skipped ({type(ex).__name__})", it.id)
                    continue
                name = arcname(e, ext)
                if name in used:
                    rows.append((it.name, e.library, e.server_path, "", "", f"skipped: same file name as {used[name]}"))
                    yield Event("warn", it.name, f"skipped: would overwrite the poster of {used[name]}", it.id)
                    continue
                used[name] = it.name
                zi = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
                zi.external_attr = 0o644 << 16
                z.writestr(zi, data)
                rows.append((it.name, e.library, e.server_path, name, source, "ok"))
                done += 1
                if i % 25 == 0:
                    yield Event("info", message=f"  {i} of {len(plan.entries)} done")
            for name, why_path, why in plan.skipped:
                rows.append((name, "", why_path, "", "", f"skipped: {why}"))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["item", "library", "server_path", "exported_as", "source", "status"])
            for r in rows:
                w.writerow([_cell(c) for c in r])
            z.writestr(f"{DIR}/manifest.csv", buf.getvalue())
            locs = sorted({loc for lib in ws_locations(ws) for loc in lib})
            z.writestr(f"{DIR}/README.txt", README.format(
                when=time.strftime("%Y-%m-%d %H:%M"), server=server_label or "the media server", n=done,
                which="as shown on the server now (with scores)" if which == "current" else "original posters (no scores)",
                locations=", ".join(locs) or "the library folders", dir=DIR,
                top_note={"location": "the name of each library's folder", "library": "the library's name",
                          "none": "absent: paths start inside each library folder, so extract into that library folder"}[layout]))
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    skipped = sum(1 for r in rows if r[5].startswith("skipped"))
    yield Event("export", dest.name, f"{done} poster(s), {dest.stat().st_size / 1e6:.1f} MB" + (f"; {skipped} skipped (see manifest.csv)" if skipped else ""))


def ws_locations(ws) -> list[list[str]]:
    try:
        return [[str(x) for x in (v.get("Locations") or [])] for v in ws.srv.libraries()]
    except Exception:
        return []


# ---- running it ------------------------------------------------------------------------------------------------
NAME_RE = re.compile(r"^laminator-posters-[a-z]+-[0-9]{8}-[0-9]{6}\.zip$")


def exports_dir(root: Path) -> Path:
    d = root / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_exports(root: Path) -> list[dict]:
    out = []
    for f in sorted(exports_dir(root).glob("laminator-posters-*.zip"), reverse=True):
        if NAME_RE.match(f.name) and f.is_file() and not f.is_symlink():
            st = f.stat()
            out.append({"name": f.name, "mb": round(st.st_size / 1e6, 1), "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def run_export(ws, root: Path, which: str = "current", layout: str = "location", naming: str = "auto", library: str | None = None,
               dest: Path | None = None, label: str = "") -> Iterator[Event]:
    """List the server's libraries and items, plan the layout, write the zip. Yields progress events."""
    libs = ws.srv.libraries()
    items = actions.all_libraries(ws.srv)
    if library and library not in items:
        raise ValueError(f"library {library!r} not found; available: {', '.join(items)}")
    plan = plan_export(libs, items, layout, naming, library)
    dest = dest or exports_dir(root) / time.strftime(f"laminator-posters-{ws.scfg.kind}-%Y%m%d-%H%M%S.zip")
    yield from build(ws, dest, plan, which, layout, label or ws.scfg.kind)
