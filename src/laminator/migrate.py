"""One-time move of state from the old single-server layout into the first server's own folder.

Before: <data>/state.db, <data>/originals/, <data>/out/, <data>/schedule.json  (one server, implied)
After:  <data>/servers/<kind>/{state.db, originals/, out/, schedule.json}      (each server on its own)

It only renames (same filesystem, nothing is copied or deleted), refuses to run if anything would be overwritten,
fixes the file paths stored inside state.db, and verifies the database afterwards. If anything looks wrong it stops and
leaves the old files where they were."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB = "state.db"
SIDE_FILES = ("state.db-wal", "state.db-shm", "schedule.json")
DIRS = ("originals", "out")


def needs_migration(root: Path, sdir: Path) -> bool:
    return (root / DB).exists() and not (sdir / DB).exists()


def migrate_legacy(root: Path, sdir: Path) -> str | None:
    """Returns a one-line description when something was moved, None when there was nothing to do.
    Raises RuntimeError (before moving anything) if the move would not be safe."""
    if not needs_migration(root, sdir):
        return None
    for d in DIRS:
        if (sdir / d).exists() and any((sdir / d).iterdir()):
            raise RuntimeError(f"{sdir / d} already has files; not overwriting. Move them aside and try again.")
    if os.stat(root / DB).st_dev != os.stat(sdir).st_dev:
        raise RuntimeError("the data folder spans filesystems; refusing a cross-device move")
    # make sure the old database is complete and readable before touching anything
    con = sqlite3.connect(root / DB)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
        before = con.execute("SELECT count(*) FROM posters").fetchone()[0]
    finally:
        con.close()

    moved = []
    try:
        for d in DIRS:
            if (root / d).is_dir():
                target = sdir / d
                if target.exists():
                    target.rmdir()  # empty (checked above)
                (root / d).rename(target)
                moved.append((root / d, target))
        for name in (DB,) + SIDE_FILES:
            if (root / name).exists():
                (root / name).rename(sdir / name)
                moved.append((root / name, sdir / name))
        con = sqlite3.connect(sdir / DB)
        try:
            for item_id, path in con.execute("SELECT item_id, original_path FROM posters").fetchall():
                new = (sdir / "originals" / Path(path).name).resolve()
                con.execute("UPDATE posters SET original_path=? WHERE item_id=?", (str(new), item_id))
            after = con.execute("SELECT count(*) FROM posters").fetchone()[0]
            missing = [p for (p,) in con.execute("SELECT original_path FROM posters") if not Path(p).exists()]
            if after != before or missing:
                con.rollback()  # the stored paths stay exactly as they were
                raise RuntimeError(f"verification failed (rows {before}->{after}, missing originals: {len(missing)})")
            con.commit()
        finally:
            con.close()
    except Exception:
        for src, dst in reversed(moved):  # put everything back
            if dst.exists() and not src.exists():
                dst.rename(src)
        raise
    return f"moved existing state ({before} poster record(s)) into {sdir}"
