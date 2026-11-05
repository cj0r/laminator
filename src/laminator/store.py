"""SQLite state: which items we changed, their original poster, and what we last applied."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_backups (
    item_id TEXT NOT NULL,
    key TEXT NOT NULL,            -- override key, e.g. rating
    name TEXT NOT NULL,
    old_value TEXT NOT NULL,      -- JSON: the value before we first changed it
    override_value TEXT NOT NULL, -- JSON: the value we last wrote
    lock_added INTEGER NOT NULL,  -- 1 if WE added the lock (so restore removes it)
    updated_at REAL NOT NULL,
    PRIMARY KEY (item_id, key)
);
CREATE TABLE IF NOT EXISTS missing (
    item_id TEXT PRIMARY KEY,
    since REAL NOT NULL           -- when maintenance first found the item gone from the server (cleanup waits out a grace period)
);
CREATE TABLE IF NOT EXISTS posters (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    original_path TEXT NOT NULL,
    original_sha TEXT NOT NULL,
    applied_hash TEXT,          -- NULL = original currently on the server
    score_text TEXT,
    served_tag TEXT,            -- Primary image tag the server had when we last looked
    served_sha TEXT,            -- sha256 of the poster bytes the server had then (what we believe it holds)
    updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path = Path("state.db")):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._keep_copy_before_leaving_wal(path)
        # PERSIST, not WAL: WAL needs a memory-mapped -shm file and truncates the -wal file, and DELETE/TRUNCATE
        # create, unlink and truncate a journal file on every write. All of those hit OpenZFS 2.4.2 bugs (kernel
        # panics on one home server, 2026-11-04). PERSIST only overwrites the journal header in place.
        # A database left in WAL mode by an older version is converted on open.
        self.db.execute("PRAGMA journal_mode=PERSIST")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(posters)")}
        if "served_tag" not in cols:
            self.db.execute("ALTER TABLE posters ADD COLUMN served_tag TEXT")
        if "served_sha" not in cols:
            self.db.execute("ALTER TABLE posters ADD COLUMN served_sha TEXT")
        self.db.commit()

    def _keep_copy_before_leaving_wal(self, path: Path) -> None:
        """A database an earlier build left in WAL mode is about to be converted (see __init__). Keep one consistent copy first, made with
        SQLite's own backup (so rows that still live only in the -wal file are in it). Made once and never overwritten."""
        try:
            if self.db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                return
            keep = Path(str(path) + ".pre-persist")
            if keep.exists():
                return
            tmp = Path(str(keep) + ".tmp")
            tmp.unlink(missing_ok=True)
            out = sqlite3.connect(tmp)
            try:
                self.db.backup(out)
            finally:
                out.close()
            tmp.replace(keep)
        except (sqlite3.Error, OSError):
            pass  # no copy is no reason to stop; the conversion itself does not change any row

    def get(self, item_id: str):
        return self.db.execute("SELECT * FROM posters WHERE item_id=?", (item_id,)).fetchone()

    def all(self):
        return self.db.execute("SELECT * FROM posters ORDER BY name").fetchall()

    def put_original(self, item_id: str, name: str, path: str, sha: str, tag: str | None) -> None:
        """First sight of an item: the server holds the untouched original right now."""
        self.db.execute(
            "INSERT INTO posters(item_id,name,original_path,original_sha,applied_hash,served_tag,served_sha,updated_at)"
            " VALUES(?,?,?,?,NULL,?,?,?)",
            (item_id, name, path, sha, tag, sha, time.time()),
        )
        self.db.commit()

    def set_original(self, item_id: str, path: str, sha: str, tag: str | None) -> None:
        """The user chose a new poster on the server: it becomes the original; no overlay on it yet."""
        self.db.execute(
            "UPDATE posters SET original_path=?, original_sha=?, applied_hash=NULL, score_text=NULL,"
            " served_tag=?, served_sha=?, updated_at=? WHERE item_id=?",
            (path, sha, tag, sha, time.time(), item_id),
        )
        self.db.commit()

    def set_served(self, item_id: str, tag: str | None, sha: str, applied_hash="keep") -> None:
        if applied_hash == "keep":
            self.db.execute("UPDATE posters SET served_tag=?, served_sha=?, updated_at=? WHERE item_id=?",
                            (tag, sha, time.time(), item_id))
        else:  # e.g. None: the overlay is gone from the server
            self.db.execute("UPDATE posters SET served_tag=?, served_sha=?, applied_hash=?, score_text=NULL,"
                            " updated_at=? WHERE item_id=?", (tag, sha, applied_hash, time.time(), item_id))
        self.db.commit()

    def set_applied(self, item_id: str, applied_hash: str | None, score_text: str | None,
                    served_tag: str | None, served_sha: str) -> None:
        self.db.execute(
            "UPDATE posters SET applied_hash=?, score_text=?, served_tag=?, served_sha=?, updated_at=? WHERE item_id=?",
            (applied_hash, score_text, served_tag, served_sha, time.time(), item_id),
        )
        self.db.commit()

    # ---- items that disappeared from the server (for cleanup) -----------------------------------------------
    def managed_ids(self) -> set[str]:
        return ({r[0] for r in self.db.execute("SELECT item_id FROM posters")}
                | {r[0] for r in self.db.execute("SELECT DISTINCT item_id FROM meta_backups")})

    def missing_map(self) -> dict[str, float]:
        return {r[0]: r[1] for r in self.db.execute("SELECT item_id, since FROM missing")}

    def mark_missing(self, ids, now: float) -> None:
        self.db.executemany("INSERT OR IGNORE INTO missing(item_id, since) VALUES(?,?)", [(i, now) for i in ids])
        self.db.commit()

    def clear_missing(self, ids) -> None:
        self.db.executemany("DELETE FROM missing WHERE item_id=?", [(i,) for i in ids])
        self.db.commit()

    def rows_for(self, item_id: str) -> dict:
        """Everything we keep about one item, as plain data (written to the trash folder before the rows are removed)."""
        return {"posters": [dict(r) for r in self.db.execute("SELECT * FROM posters WHERE item_id=?", (item_id,))],
                "meta_backups": [dict(r) for r in self.db.execute("SELECT * FROM meta_backups WHERE item_id=?", (item_id,))]}

    def forget_item(self, item_id: str) -> None:
        for t in ("posters", "meta_backups", "missing"):
            self.db.execute(f"DELETE FROM {t} WHERE item_id=?", (item_id,))
        self.db.commit()

    # ---- metadata backups -------------------------------------------------
    def meta_get(self, item_id: str) -> dict:
        rows = self.db.execute("SELECT * FROM meta_backups WHERE item_id=?", (item_id,)).fetchall()
        return {r["key"]: r for r in rows}

    def meta_all(self):
        return self.db.execute("SELECT * FROM meta_backups ORDER BY name, key").fetchall()

    def meta_put(self, item_id, name, key, old, override, lock_added) -> None:
        import json
        self.db.execute(
            "INSERT OR REPLACE INTO meta_backups VALUES(?,?,?,?,?,?,?)",
            (item_id, key, name, json.dumps(old), json.dumps(override), int(lock_added), time.time()),
        )
        self.db.commit()

    def meta_delete(self, item_id: str, key: str) -> None:
        self.db.execute("DELETE FROM meta_backups WHERE item_id=? AND key=?", (item_id, key))
        self.db.commit()


# ---- helpers for the settings UI -------------------------------------------------------------------------
def state_counts(sdir: Path) -> tuple[int, int]:
    """(poster records, metadata records) kept for the server whose folder this is; (0, 0) when there is no state."""
    f = sdir / "state.db"
    if not f.exists():
        return 0, 0
    st = Store(f)
    try:
        return len(st.all()), len(st.meta_all())
    finally:
        st.db.close()


def archive_state(sdir: Path) -> Path | None:
    """Move a server's state (database, saved originals, outputs, schedule record) into archive-<time>/ inside its own
    folder. Nothing is deleted: it is how a changed server address starts clean without losing the old restore data."""
    names = ("state.db", "state.db-wal", "state.db-shm", "originals", "out", "schedule.json")
    present = [sdir / n for n in names if (sdir / n).exists()]
    if not present:
        return None
    dest = sdir / time.strftime("archive-%Y%m%d-%H%M%S")
    dest.mkdir()
    for p in present:
        p.rename(dest / p.name)
    return dest
