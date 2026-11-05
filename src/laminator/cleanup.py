"""Clean up what laminator no longer needs, without ever risking restore data on a guess.

What it removes, and how:
  * records and saved originals of items that are GONE from the server: only after the item has stayed missing for the grace period
    (default 30 days), and moved to trash/ (with a copy of the database rows), not deleted.
  * saved originals that no record points to (orphans): moved to trash/.
  * regenerable things, deleted outright: old dry-run renders (out/), old poster-preview caches, old MDBList caches, old export zips.
  * trash older than the retention (default 90 days): deleted for good. This is the only permanent loss of anything that mattered.
  * optional (off by default): pre-restore-* and archive-* safety copies older than 180 days, moved to trash/.

"Gone" is decided carefully: the server's items must be listed successfully, and if more than half of the managed items vanish at once
(a library offline, a mount missing) that is treated as an outage, not 50 deletions, and nothing is marked. Server trouble never deletes anything.
Only files directly inside laminator's own folders are touched; symlinks and anything with an unexpected name are left alone."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import actions
from .actions import Event

DAY = 86400
ORIG_RE = re.compile(r"^(?P<id>[A-Za-z0-9]+?)(?:-(?P<h>[0-9a-f]{8}))?\.img$")


@dataclass
class Limits:
    grace_days: int = 30
    outputs_days: int = 30
    poster_cache_days: int = 14
    mdb_cache_days: int = 7
    exports_days: int = 14
    trash_days: int = 90
    safety_days: int | None = None  # opt-in (e.g. 180): move old pre-restore-*/archive-* folders to trash

    @classmethod
    def from_cfg(cls, cfg: dict, safety_days: int | None = None) -> "Limits":
        """The `cleanup:` section of the config (or saved Settings): grace_days (7-365) and trash_days (7-730)."""
        from .config import ConfigError
        c = cfg.get("cleanup") or {}
        if not isinstance(c, dict):
            raise ConfigError("cleanup: must be a mapping")
        out = cls(safety_days=safety_days)
        for key, lo, hi in (("grace_days", 7, 365), ("trash_days", 7, 730)):
            if key in c:
                v = c[key]
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    raise ConfigError(f"cleanup.{key} must be a whole number of days from {lo} to {hi}")
                setattr(out, key, v)
        return out


@dataclass
class Gone:
    item_id: str
    name: str
    since: float
    files: list[Path]
    bytes: int


@dataclass
class Plan:
    server_ok: bool = True
    problem: str = ""
    gone: list[Gone] = field(default_factory=list)
    waiting: list[tuple[str, str, float]] = field(default_factory=list)  # (id, name, days still to wait): missing but within the grace period
    orphans: list[Path] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    caches: list[Path] = field(default_factory=list)
    exports: list[Path] = field(default_factory=list)
    trash: list[Path] = field(default_factory=list)
    safety: list[Path] = field(default_factory=list)
    newly_missing: int = 0
    returned: int = 0

    @property
    def empty(self) -> bool:
        return not any((self.gone, self.orphans, self.outputs, self.caches, self.exports, self.trash, self.safety))


def size(p: Path) -> int:
    try:
        if p.is_symlink():
            return 0
        return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
    except OSError:
        return 0


def _old(files, now: float, days: int) -> list[Path]:
    return [f for f in files if f.is_file() and not f.is_symlink() and now - f.stat().st_mtime > days * DAY]


def _old_dirs(parent: Path, pattern: str, now: float, days: int) -> list[Path]:
    if not parent.is_dir():
        return []
    return [d for d in parent.glob(pattern) if d.is_dir() and not d.is_symlink() and now - d.stat().st_mtime > days * DAY]


def _item_files(orig: Path, item_id: str) -> list[Path]:
    out = []
    for f in orig.glob(f"{item_id}*.img") if orig.is_dir() else []:
        m = ORIG_RE.match(f.name)
        if m and m["id"] == item_id and f.is_file() and not f.is_symlink():
            out.append(f)
    return out


# ---- planning ------------------------------------------------------------------------------------------------
def scan_server(ws, now: float, lim: Limits, mark: bool = True) -> Plan:
    """What could be cleaned for ONE server. Talks to the server read-only; with mark=True it also records which managed items are
    missing (bookkeeping only; nothing is removed here)."""
    plan = Plan()
    orig = ws.data / "originals"
    managed = ws.store.managed_ids()
    try:
        present = {it.id for items in actions.all_libraries(ws.srv).values() for it in items}
    except Exception as e:  # any trouble listing means we do not know what is gone: touch nothing about items
        plan.server_ok, plan.problem = False, f"could not list the server ({type(e).__name__}); items were not checked"
        present = None
    if present is not None:
        gone_now = managed - present
        if not present and managed:
            plan.server_ok, plan.problem = False, "the server returned no items at all; treating that as an outage, not as deletions"
        elif len(gone_now) >= 3 and len(gone_now) > 0.5 * len(managed):
            plan.server_ok, plan.problem = False, (f"{len(gone_now)} of {len(managed)} managed items are missing at once; that looks like a library "
                                                  "or mount being offline, so nothing was marked or cleaned")
        if plan.server_ok:
            miss = ws.store.missing_map()
            back = [i for i in miss if i in present]
            new = [i for i in gone_now if i not in miss]
            plan.returned, plan.newly_missing = len(back), len(new)
            if mark:
                ws.store.clear_missing(back)
                ws.store.mark_missing(new, now)
                miss = ws.store.missing_map()
            names = {}
            for i in gone_now:
                rows = ws.store.rows_for(i)
                names[i] = (rows["posters"][0]["name"] if rows["posters"] else rows["meta_backups"][0]["name"] if rows["meta_backups"] else i)
            for i in sorted(gone_now):
                since = miss.get(i, now)
                if now - since >= lim.grace_days * DAY:
                    files = _item_files(orig, i)
                    plan.gone.append(Gone(i, names[i], since, files, sum(size(f) for f in files)))
                else:
                    plan.waiting.append((i, names[i], round(lim.grace_days - (now - since) / DAY, 1)))
            # saved originals nothing points to (and that do not belong to an item we are still waiting on)
            keep = managed | set()
            if orig.is_dir():
                for f in orig.glob("*.img"):
                    m = ORIG_RE.match(f.name)
                    if m and f.is_file() and not f.is_symlink() and m["id"] not in keep:
                        plan.orphans.append(f)
    out = ws.data / "out"
    plan.outputs = _old(out.glob("*") if out.is_dir() else [], now, lim.outputs_days)
    plan.trash = _old_dirs(ws.data / "trash", "*", now, lim.trash_days)
    if lim.safety_days:
        plan.safety = _old_dirs(ws.data, "archive-*", now, lim.safety_days)
    return plan


def scan_root(root: Path, now: float, lim: Limits) -> Plan:
    """The shared folders: caches, old export zips, the root trash, optional old pre-restore-* copies."""
    plan = Plan()
    plan.caches = (_old((root / "cache" / "posters").glob("*.img") if (root / "cache" / "posters").is_dir() else [], now, lim.poster_cache_days)
                   + _old((root / "cache" / "mdblist").glob("*.json") if (root / "cache" / "mdblist").is_dir() else [], now, lim.mdb_cache_days)
                   + _old((root / "cache" / "thumbs").glob("*.jpg") if (root / "cache" / "thumbs").is_dir() else [], now, lim.poster_cache_days))
    plan.exports = _old((root / "exports").glob("laminator-posters-*.zip") if (root / "exports").is_dir() else [], now, lim.exports_days)
    plan.trash = _old_dirs(root / "trash", "*", now, lim.trash_days)
    if lim.safety_days:
        plan.safety = _old_dirs(root, "pre-restore-*", now, lim.safety_days)
    return plan


# ---- doing it ------------------------------------------------------------------------------------------------------
def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except OSError:
        return False


def _delete(paths: list[Path], base: Path) -> int:
    n = 0
    for p in paths:
        if p.is_symlink() or not _inside(p, base):
            continue  # never follow a link out of our own folder
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _move(paths: list[Path], dest_dir: Path, base: Path) -> int:
    n = 0
    for p in paths:
        if p.is_symlink() or not _inside(p, base):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / p.name
        k = 1
        while target.exists():
            target = dest_dir / f"{p.name}.{k}"
            k += 1
        try:
            p.rename(target)
            n += 1
        except OSError:
            pass
    return n


def _mb(b: int) -> str:
    return f"{b / 1e6:.1f} MB"


def apply_server(ws, plan: Plan, now: float) -> Iterator[Event]:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    tdir = ws.data / "trash" / stamp
    if plan.gone:
        rows = {g.item_id: ws.store.rows_for(g.item_id) | {"name": g.name, "missing_since": g.since} for g in plan.gone}
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "rows.json").write_text(json.dumps(rows, indent=1, default=str))  # written BEFORE the rows are removed
        (tdir / "README.txt").write_text("Items that disappeared from the server and stayed missing past the grace period.\n"
                                         "originals/ has their saved posters; rows.json has the database records that were removed.\n"
                                         "Deleted for good by maintenance after the trash retention period.\n")
        moved = 0
        for g in plan.gone:
            moved += _move(g.files, tdir / "originals", ws.data / "originals")
            ws.store.forget_item(g.item_id)
        yield Event("cleanup", ws.scfg.kind, f"{len(plan.gone)} item(s) gone from the server for over the grace period: {moved} saved poster(s) "
                                              f"({_mb(sum(g.bytes for g in plan.gone))}) and their records moved to trash/{stamp}")
    if plan.orphans:
        n = _move(plan.orphans, tdir / "originals", ws.data / "originals")
        yield Event("cleanup", ws.scfg.kind, f"{n} saved poster file(s) that nothing refers to moved to trash/{stamp}")
    if plan.safety:
        n = _move(plan.safety, tdir / "safety-copies", ws.data)
        yield Event("cleanup", ws.scfg.kind, f"{n} old archive folder(s) moved to trash/{stamp}")
    if plan.outputs:
        b = sum(size(p) for p in plan.outputs)
        yield Event("cleanup", ws.scfg.kind, f"{_delete(plan.outputs, ws.data / 'out')} old render(s) deleted ({_mb(b)})")
    if plan.trash:
        b = sum(size(p) for p in plan.trash)
        yield Event("cleanup", ws.scfg.kind, f"{_delete(plan.trash, ws.data / 'trash')} trash folder(s) past their retention deleted for good ({_mb(b)})")
    if tdir.exists():
        os.utime(tdir, (now, now))  # the retention clock starts when something is moved into the trash
    if plan.waiting:
        yield Event("info", message=f"  {len(plan.waiting)} item(s) missing from {ws.scfg.kind} but still inside the grace period; nothing done yet")


def apply_root(root: Path, plan: Plan, now: float) -> Iterator[Event]:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    if plan.caches:
        b = sum(size(p) for p in plan.caches)
        yield Event("cleanup", "cache", f"{_delete(plan.caches, root / 'cache')} old cache file(s) deleted ({_mb(b)})")
    if plan.exports:
        b = sum(size(p) for p in plan.exports)
        yield Event("cleanup", "exports", f"{_delete(plan.exports, root / 'exports')} old export package(s) deleted ({_mb(b)})")
    if plan.safety:
        n = _move(plan.safety, root / "trash" / stamp, root)
        if (root / "trash" / stamp).exists():
            os.utime(root / "trash" / stamp, (now, now))
        yield Event("cleanup", "restore", f"{n} old pre-restore folder(s) moved to trash/{stamp}")
    if plan.trash:
        b = sum(size(p) for p in plan.trash)
        yield Event("cleanup", "trash", f"{_delete(plan.trash, root / 'trash')} trash folder(s) past their retention deleted for good ({_mb(b)})")


def run(ws, now: float | None = None, lim: Limits | None = None, dry_run: bool = False) -> Iterator[Event]:
    """Scan then (unless dry_run) clean one server plus the shared folders. Yields events."""
    now, lim = now or time.time(), lim or Limits()
    sp, rp = scan_server(ws, now, lim, mark=not dry_run), scan_root(ws.root, now, lim)
    if sp.problem:
        yield Event("warn", ws.scfg.kind, sp.problem)
    if dry_run:
        yield from describe(sp, rp)
        return
    yield from apply_server(ws, sp, now)
    yield from apply_root(ws.root, rp, now)
    if sp.empty and rp.empty and not sp.waiting:
        yield Event("cleanup", ws.scfg.kind, "nothing to clean up")


def describe(sp: Plan, rp: Plan) -> Iterator[Event]:
    for g in sp.gone:
        yield Event("info", message=f"  gone from the server, past the grace period: {g.name} ({len(g.files)} saved poster(s), {_mb(g.bytes)}) -> trash")
    for i, name, days in sp.waiting:
        yield Event("info", message=f"  missing from the server, waiting {days} more day(s): {name}")
    for label, items in (("orphan saved posters -> trash", sp.orphans), ("old renders -> deleted", sp.outputs), ("old archive folders -> trash", sp.safety),
                         ("trash past retention -> deleted for good", sp.trash + rp.trash), ("old caches -> deleted", rp.caches),
                         ("old export packages -> deleted", rp.exports), ("old pre-restore folders -> trash", rp.safety)):
        if items:
            yield Event("info", message=f"  {len(items)} {label} ({_mb(sum(size(p) for p in items))})")
    if sp.empty and rp.empty and not sp.waiting:
        yield Event("info", message="  nothing to clean up")
