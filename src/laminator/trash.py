"""Look into trash/ and put things back.

trash/<time>/ is written by cleanup: originals/ (saved posters), rows.json (the database records removed with them), safety-copies/.
Restoring never overwrites: an item that has records again is skipped, a file that exists is kept (an identical one just drops the
trash copy). rows.json is treated as untrusted input: only known columns, strict types, and every poster path is re-pointed at this
server's own originals folder (a record whose picture is not there is not restored)."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import actions
from .actions import Event
from .cleanup import DAY, ORIG_RE, Limits, size
from .meta import _ALL

STAMP_RE = re.compile(r"^\d{8}-\d{6}$")
POSTER_COLS = {"item_id": str, "name": str, "original_path": str, "original_sha": str, "applied_hash": (str, type(None)),
               "score_text": (str, type(None)), "served_tag": (str, type(None)), "served_sha": (str, type(None)), "updated_at": (int, float)}
META_COLS = {"item_id": str, "key": str, "name": str, "old_value": str, "override_value": str, "lock_added": int, "updated_at": (int, float)}


@dataclass
class TItem:
    item_id: str
    name: str
    files: list[Path]
    bytes: int
    missing_since: float | None = None
    has_rows: bool = False
    done: bool = False  # set once its records were really put back


@dataclass
class Batch:
    stamp: str
    path: Path
    scope: str  # "server" (this server's trash) or "root" (shared trash: pre-restore folders)
    items: list[TItem] = field(default_factory=list)
    orphans: list[Path] = field(default_factory=list)
    safety: list[Path] = field(default_factory=list)
    bytes: int = 0
    age_days: float = 0.0
    keep_days_left: float = 0.0


def _rows(path: Path) -> dict:
    try:
        d = json.loads((path / "rows.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _batch(path: Path, scope: str, now: float, lim: Limits) -> Batch | None:
    if not STAMP_RE.match(path.name) or not path.is_dir() or path.is_symlink():
        return None
    b = Batch(path.name, path, scope)
    rows = _rows(path)
    by_id: dict[str, list[Path]] = {}
    orig = path / "originals"
    for f in sorted(orig.glob("*.img")) if orig.is_dir() else []:
        m = ORIG_RE.match(f.name)
        if m and f.is_file() and not f.is_symlink():
            by_id.setdefault(m["id"], []).append(f)
    for i, files in by_id.items():
        if i in rows and isinstance(rows[i], dict):
            b.items.append(TItem(i, str(rows[i].get("name") or i)[:200], files, sum(size(f) for f in files), rows[i].get("missing_since"), True))
        else:
            b.orphans += files
    for i, r in rows.items():  # records whose posters are gone from the trash: still restorable (records only)
        if isinstance(r, dict) and i not in by_id and (r.get("posters") or r.get("meta_backups")):
            b.items.append(TItem(i, str(r.get("name") or i)[:200], [], 0, r.get("missing_since"), True))
    sc = path / "safety-copies"
    b.safety = [d for d in sorted(sc.iterdir()) if d.is_dir() and not d.is_symlink()] if sc.is_dir() else []
    if scope == "root":
        b.safety += [d for d in sorted(path.glob("pre-restore-*")) if d.is_dir() and not d.is_symlink()]
    b.bytes = size(path)
    b.age_days = round((now - path.stat().st_mtime) / DAY, 1)
    b.keep_days_left = round(lim.trash_days - b.age_days, 1)
    return b


def list_batches(ws, now: float | None = None, lim: Limits | None = None) -> list[Batch]:
    """Every trash batch for this server (and the shared root trash), newest first."""
    now, lim = now or time.time(), lim or Limits()
    out = []
    for base, scope in ((ws.data / "trash", "server"), (ws.root / "trash", "root")):
        if base.is_dir() and not base.is_symlink():
            out += [b for d in base.iterdir() if (b := _batch(d, scope, now, lim))]
    return sorted(out, key=lambda b: b.stamp, reverse=True)


def _valid(row: dict, cols: dict) -> bool:
    return all(k in row and isinstance(row[k], t) and not isinstance(row[k], bool) for k, t in cols.items())


def _restore_item(ws, b: Batch, it: TItem, rows: dict) -> Iterator[Event]:
    if it.item_id in ws.store.managed_ids():
        yield Event("warn", it.name, "already has records again; not restoring over them", it.item_id)
        return
    orig = ws.originals
    moved = 0
    for f in it.files:
        dest = orig / f.name
        if dest.exists():
            if actions.sha(dest.read_bytes()) == actions.sha(f.read_bytes()):
                f.unlink()  # the identical picture is already back: just drop the trash copy
            else:
                yield Event("warn", it.name, f"{f.name} exists with different content; kept the trash copy", it.item_id)
            continue
        f.rename(dest)
        moved += 1
    r = rows.get(it.item_id) or {}
    n_rows, left_p, left_m = 0, [], []
    for p in (r.get("posters") or []):
        if not (isinstance(p, dict) and _valid(p, POSTER_COLS) and p["item_id"] == it.item_id):
            continue  # malformed or for another item: can never be restored, so it is not kept either
        path = orig / Path(p["original_path"]).name  # re-pointed at THIS server's originals folder, never the stored path
        if not ORIG_RE.match(path.name) or not path.is_file():
            yield Event("warn", it.name, "a record's saved poster is not there; that record was not restored (it stays in the trash)", it.item_id)
            left_p.append(p)
            continue
        ws.store.db.execute("INSERT OR IGNORE INTO posters(item_id,name,original_path,original_sha,applied_hash,score_text,served_tag,served_sha,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?)", (p["item_id"], p["name"], str(path), p["original_sha"], p["applied_hash"], p["score_text"],
                                                           p["served_tag"], p["served_sha"], p["updated_at"]))
        n_rows += 1
    for m in (r.get("meta_backups") or []):
        if isinstance(m, dict) and _valid(m, META_COLS) and m["item_id"] == it.item_id and m["key"] in _ALL:
            ws.store.db.execute("INSERT OR IGNORE INTO meta_backups VALUES(?,?,?,?,?,?,?)",
                                (m["item_id"], m["key"], m["name"], m["old_value"], m["override_value"], m["lock_added"], m["updated_at"]))
            n_rows += 1
    ws.store.db.commit()
    if left_p or left_m:
        rows[it.item_id] = {**r, "posters": left_p, "meta_backups": left_m}  # only what could not be restored stays behind
    else:
        rows.pop(it.item_id, None)
        it.done = True
    yield Event("restore", it.name, f"{moved} saved poster(s) and {n_rows} record(s) back from the trash"
                                    + " (they are cleaned up again if the item is still gone from the server after the grace period)", it.item_id)


def restore(ws, stamp: str, item_id: str | None = None, now: float | None = None, lim: Limits | None = None) -> Iterator[Event]:
    """Restore one item, or everything, from one trash batch. Nothing is overwritten."""
    if not STAMP_RE.match(stamp):
        raise ValueError("bad trash name")
    batches = {(b.stamp, b.scope): b for b in list_batches(ws, now, lim)}
    mine = [b for (st, sc), b in batches.items() if st == stamp]
    if not mine:
        raise FileNotFoundError(f"no trash batch {stamp}")
    for b in mine:
        rows = _rows(b.path)
        todo = [it for it in b.items if item_id in (None, it.item_id)]
        if item_id and not todo and not (b.orphans and item_id == "orphans"):
            continue
        for it in todo:
            yield from _restore_item(ws, b, it, rows)  # updates `rows` itself: only what was really restored leaves rows.json
        if item_id is None:
            for f in b.orphans:
                dest = ws.originals / f.name
                if not dest.exists():
                    f.rename(dest)
                    yield Event("restore", f.name, "saved poster put back", "")
            for d in b.safety:
                dest = (ws.data if b.scope == "server" else ws.root) / d.name
                if dest.exists():
                    yield Event("warn", d.name, "already exists; kept the trash copy")
                else:
                    shutil.move(str(d), str(dest))
                    yield Event("restore", d.name, "safety copy put back")
        if todo:
            (b.path / "rows.json").write_text(json.dumps(rows, indent=1, default=str))
        left = [p for p in b.path.rglob("*") if p.is_file() and p.name not in ("README.txt", "rows.json")]
        if not left and not rows and b.path.exists():
            shutil.rmtree(b.path)  # everything in it is back: the empty batch goes
            yield Event("info", message=f"  trash batch {b.stamp} is empty now and was removed")
