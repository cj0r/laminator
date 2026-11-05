"""The state database must not use WAL: WAL's memory-mapped -shm file and its truncation of the -wal file were in the
path of OpenZFS 2.4.2 kernel panics (a home server, 2026-11-04). PERSIST only overwrites the journal header in place.
Only WAL is remembered in the database file; PERSIST is a per-connection setting, so it is checked on the Store's own connection."""
import sqlite3

from laminator.store import Store


def file_mode(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


def test_new_database_uses_persist_and_no_wal_files(tmp_path):
    db = tmp_path / "state.db"
    st = Store(db)
    assert st.db.execute("PRAGMA journal_mode").fetchone()[0] == "persist"
    st.put_original("1", "A", str(tmp_path / "a.img"), "s", "t")
    assert file_mode(db) != "wal"
    assert not (tmp_path / "state.db-wal").exists() and not (tmp_path / "state.db-shm").exists()
    st.db.close()


def test_old_wal_database_is_converted_and_keeps_its_rows(tmp_path):
    db = tmp_path / "state.db"
    st = Store(db)
    st.db.execute("PRAGMA journal_mode=WAL")
    st.put_original("1", "A", str(tmp_path / "a.img"), "s", "t")
    st.db.close()
    assert file_mode(db) == "wal"
    st = Store(db)
    assert st.db.execute("PRAGMA journal_mode").fetchone()[0] == "persist"
    assert st.get("1") is not None
    st.db.close()
    assert file_mode(db) != "wal"
    assert not (tmp_path / "state.db-wal").exists() and not (tmp_path / "state.db-shm").exists()
