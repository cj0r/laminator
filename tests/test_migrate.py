import sqlite3
from pathlib import Path

import pytest

from laminator import migrate
from laminator.store import Store


def legacy(root: Path):
    """An old-layout data folder: a populated state.db (with relative original paths, as before), originals/, schedule.json."""
    (root / "originals").mkdir(parents=True)
    (root / "out").mkdir()
    (root / "originals" / "8.img").write_bytes(b"ORIGINAL-8")
    (root / "originals" / "8-abcd1234.img").write_bytes(b"ADOPTED")
    (root / "out" / "Elf_8.jpg").write_bytes(b"x")
    (root / "schedule.json").write_text('{"last_full": 1}')
    st = Store(root / "state.db")
    st.put_original("8", "Elf", "originals/8.img", "sha8", "tag8")          # relative path, like the old code wrote
    st.put_original("9", "Other", "originals/8-abcd1234.img", "sha9", "tag9")
    st.meta_put("8", "Elf", "critic_rating", None, 73, True)
    st.db.close()


def test_state_moves_into_the_server_folder_and_paths_are_fixed(tmp_path):
    root, sdir = tmp_path, tmp_path / "servers" / "emby"
    legacy(root); sdir.mkdir(parents=True)
    msg = migrate.migrate_legacy(root, sdir)
    assert "2 poster record(s)" in msg
    assert not (root / "state.db").exists() and not (root / "originals").exists() and not (root / "schedule.json").exists()
    assert (sdir / "originals" / "8.img").read_bytes() == b"ORIGINAL-8" and (sdir / "schedule.json").exists()
    st = Store(sdir / "state.db")
    rows = {r["item_id"]: r for r in st.all()}
    assert Path(rows["8"]["original_path"]) == (sdir / "originals" / "8.img").resolve()
    assert Path(rows["9"]["original_path"]).read_bytes() == b"ADOPTED"          # the adopted-poster file still resolves
    assert "critic_rating" in st.meta_get("8") and rows["8"]["served_tag"] == "tag8"   # nothing else about the rows changed


def test_nothing_to_do_and_idempotent(tmp_path):
    sdir = tmp_path / "servers" / "emby"; sdir.mkdir(parents=True)
    assert migrate.migrate_legacy(tmp_path, sdir) is None                      # fresh install
    legacy(tmp_path)
    assert migrate.migrate_legacy(tmp_path, sdir)
    assert migrate.migrate_legacy(tmp_path, sdir) is None                      # second run: already done


def test_refuses_to_overwrite_and_leaves_the_old_files_alone(tmp_path):
    root, sdir = tmp_path, tmp_path / "servers" / "emby"
    legacy(root); (sdir / "originals").mkdir(parents=True); (sdir / "originals" / "precious.img").write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="not overwriting"):
        migrate.migrate_legacy(root, sdir)
    assert (root / "state.db").exists() and (root / "originals" / "8.img").exists()      # untouched
    assert (sdir / "originals" / "precious.img").read_bytes() == b"keep"


def test_a_failed_verification_puts_everything_back(tmp_path, monkeypatch):
    root, sdir = tmp_path, tmp_path / "servers" / "emby"
    legacy(root); sdir.mkdir(parents=True)
    (root / "originals" / "8.img").unlink()                                      # an original is missing: verification must fail
    with pytest.raises(RuntimeError, match="verification failed"):
        migrate.migrate_legacy(root, sdir)
    assert (root / "state.db").exists() and (root / "originals" / "8-abcd1234.img").exists() and (root / "schedule.json").exists()
    assert not (sdir / "state.db").exists()
    con = sqlite3.connect(root / "state.db")                                      # and the database is back as it was
    assert con.execute("SELECT count(*) FROM posters").fetchone()[0] == 2
    paths = dict(con.execute("SELECT item_id, original_path FROM posters").fetchall())
    assert paths == {"8": "originals/8.img", "9": "originals/8-abcd1234.img"}      # the stored paths were not rewritten
